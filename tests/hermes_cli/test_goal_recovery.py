"""Durable goal recovery: real SQLite state, competing managers and finite retries."""

from unittest.mock import patch
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from hermes_cli import goals


def test_an_inflight_judge_cannot_overwrite_a_newer_user_pause():
    mgr = goals.GoalManager("judge-pause-race")
    mgr.set("finish the task")

    def pause_while_judging(*args, **kwargs):
        goals.GoalManager(mgr.session_id).pause()
        return "continue", "more work remains", False, None, False

    with patch.object(goals, "judge_goal", side_effect=pause_while_judging):
        result = mgr.evaluate_after_turn("completed one useful step")
    assert goals.load_goal(mgr.session_id).status == "paused"
    assert not result["should_continue"]

    mgr.resume()
    db = goals._get_session_db()
    original_write = db._write_rowcount
    paused = False

    def pause_at_commit(sql, params=(), **kwargs):
        nonlocal paused
        if not paused:
            paused = True
            goals.GoalManager(mgr.session_id).pause()
        return original_write(sql, params, **kwargs)

    with patch.object(db, "_write_rowcount", side_effect=pause_at_commit), patch.object(
        goals, "judge_goal", return_value=("done", "all verified", False, None, False)
    ):
        result = mgr.evaluate_after_turn("verified result")
    assert goals.load_goal(mgr.session_id).status == "paused"
    assert result["status"] != "done"
    assert not result["should_continue"]


def test_durable_intent_lifecycle_survives_reload_and_limits_failed_turns():
    sid = "pending-recovery"
    mgr = goals.GoalManager(sid)
    mgr.set("finish the task")
    intent = goals.GoalManager(sid).get_pending_continuation()
    assert intent["kind"] == "kickoff"
    goals._get_session_db().set_meta("goal:bad-json", "{")
    assert sid in goals.list_pending_goal_session_ids()
    claimed = mgr.claim_continuation(intent["id"])
    assert claimed["claim_token"]
    assert goals.GoalManager(sid).claim_continuation(intent["id"]) is None
    assert goals.GoalManager(sid).claim_continuation(intent["id"], recover=True) is None
    assert not mgr.acknowledge_continuation(intent["id"], "wrong-token")
    assert mgr.release_continuation(intent["id"], claimed["claim_token"])
    child = subprocess.run([
        sys.executable, "-c",
        "from hermes_cli.goals import GoalManager; import sys; "
        "assert GoalManager(sys.argv[1]).claim_continuation(sys.argv[2]); print('claimed')",
        sid, intent["id"],
    ], capture_output=True, text=True, timeout=30, check=True)
    assert child.stdout.strip() == "claimed"
    code = (
        "from hermes_cli.goals import GoalManager; import sys; "
        "mgr=GoalManager(sys.argv[1]); sys.stdin.readline(); "
        "claim=mgr.claim_continuation(sys.argv[2], recover=True); "
        "print('claimed' if claim else 'refused', flush=True); sys.stdin.readline()"
    )
    contenders = [subprocess.Popen(
        [sys.executable, "-c", code, sid, intent["id"]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for _ in range(2)]
    try:
        for process in contenders:
            process.stdin.write("go\n")
            process.stdin.flush()
        with ThreadPoolExecutor(max_workers=2) as pool:
            readers = [pool.submit(process.stdout.readline) for process in contenders]
            assert sorted(reader.result(timeout=30).strip() for reader in readers) == ["claimed", "refused"]
        assert goals.GoalManager(sid).claim_continuation(intent["id"], recover=True) is None
    finally:
        for process in contenders:
            if process.poll() is None:
                process.stdin.write("finish\n")
                process.stdin.flush()
            _, error = process.communicate(timeout=30)
            assert process.returncode == 0, error
    recovered = goals.GoalManager(sid).claim_continuation(intent["id"], recover=True)
    assert mgr.acknowledge_continuation(intent["id"], recovered["claim_token"])
    assert mgr.get_pending_continuation() is None

    for attempt in range(1, 4):
        mgr = goals.GoalManager(sid)
        decision = mgr.record_turn_failure({
            "failed": True, "failure_retryable": True,
            "failure_reason": "provider_temporarily_unavailable",
        })
        pending = decision["pending_continuation"]
        assert pending["attempt"] == attempt
        assert not decision["should_continue"]
        assert mgr.state.turns_used == 0
        assert mgr.claim_continuation(pending["id"], now=pending["due_at"] - 1) is None
        taken = mgr.claim_continuation(pending["id"], now=pending["due_at"])
        assert taken
        assert mgr.acknowledge_continuation(taken["id"], taken["claim_token"])
    mgr = goals.GoalManager(sid)
    exhausted = mgr.record_turn_failure({"failed": True, "failure_retryable": True})
    assert exhausted["pending_continuation"] is None
    assert goals.load_goal(sid).status == "paused"

    mgr.resume()
    old = mgr.get_pending_continuation()
    peer = goals.GoalManager(sid)
    peer.set("a replacement goal")
    assert mgr.claim_continuation(old["id"]) is None
    peer.pause()
    assert goals.GoalManager(sid).get_pending_continuation() is None
    peer.resume()
    hard = peer.record_turn_failure({
        "failed": True, "failure_retryable": False, "failure_reason": "authentication_failed",
    })
    assert hard["pending_continuation"] is None
    assert goals.load_goal(sid).status == "paused"

    good = goals.GoalManager("valid-sibling")
    good.set("continue valid work")
    bad = goals.GoalState(goal="broken deadline", goal_id="g", pending_continuation={
        "id": "invalid-deadline", "goal_id": "g", "prompt": "resume", "due_at": "bogus",
    })
    goals.save_goal("invalid-deadline", bad)
    assert goals.GoalManager("invalid-deadline").claim_continuation("invalid-deadline") is None
    assert goals.load_goal("invalid-deadline").status == "paused"
    not_finite = goals.GoalState(goal="nonfinite deadline", goal_id="g", pending_continuation={
        "id": "nonfinite-deadline", "goal_id": "g", "prompt": "resume", "due_at": float("nan"),
    })
    goals.save_goal("nonfinite-deadline", not_finite)
    assert goals.GoalManager("nonfinite-deadline").claim_continuation("nonfinite-deadline") is None
    assert goals.load_goal("nonfinite-deadline").status == "paused"
    assert "valid-sibling" in goals.list_pending_goal_session_ids()

    good.wait_for_seconds(30, "pending work")
    good.prepare_continuation(good.next_continuation_prompt(), kind="wait", due_at=good.state.waiting_until)
    assert good.stop_waiting()
    ready = good.get_pending_continuation()
    assert good.claim_continuation(ready["id"])
