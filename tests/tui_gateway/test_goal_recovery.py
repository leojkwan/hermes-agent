"""Durable Desktop/TUI goal continuation lifecycle regressions."""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_goal_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def test_goal_continuation_is_durable_and_claimable_after_process_boundary(isolated_goal_home):
    """A judge decision survives a fresh GoalManager instead of living in session memory."""
    from hermes_cli.goals import GoalManager

    session_key = "desktop-goal-recovery"
    manager = GoalManager(session_key)
    manager.set("finish the task")
    prepared = manager.prepare_continuation("continue the task", reason="still pending")
    assert prepared and prepared["prompt"] == "continue the task"

    fresh = GoalManager(session_key)
    pending = fresh.get_pending_continuation()
    assert pending and pending["id"] == prepared["id"]

    claimed = fresh.claim_continuation(prepared["id"], recover=True)
    assert claimed and claimed["claim_token"]
    assert fresh.acknowledge_continuation(prepared["id"], claimed["claim_token"])
    assert GoalManager(session_key).get_pending_continuation() is None


def test_failed_pre_marker_claim_is_released_for_resume(isolated_goal_home):
    """A dispatch death before the durable turn marker does not consume intent."""
    from hermes_cli.goals import GoalManager

    session_key = "desktop-goal-pre-marker"
    manager = GoalManager(session_key)
    manager.set("finish the task")
    prepared = manager.prepare_continuation("continue the task")
    claimed = manager.claim_continuation(prepared["id"])
    assert claimed
    assert manager.release_continuation(prepared["id"], claimed["claim_token"])
    assert GoalManager(session_key).get_pending_continuation()["id"] == prepared["id"]


def test_two_resume_processes_claim_one_sqlite_pending_intent(isolated_goal_home):
    """Two recovery doors share one CAS claim; only one may dispatch."""
    from hermes_cli.goals import GoalManager

    session_key = "desktop-goal-two-resumes"
    manager = GoalManager(session_key)
    manager.set("finish the task")
    pending = manager.prepare_continuation("continue the task")
    assert pending

    gate = threading.Barrier(3)
    claims = []

    def resume() -> None:
        gate.wait()
        claims.append(
            GoalManager(session_key).claim_continuation(pending["id"], recover=True)
        )

    workers = [threading.Thread(target=resume) for _ in range(2)]
    for worker in workers:
        worker.start()
    gate.wait()
    for worker in workers:
        worker.join(timeout=5)

    winners = [claim for claim in claims if claim]
    assert len(winners) == 1
    assert GoalManager(session_key).get_pending_continuation()["claim_token"] == winners[0]["claim_token"]


def test_profile_a_b_a_keeps_same_sid_isolated_and_foreign_pending_unrunnable(
    isolated_goal_home, monkeypatch, tmp_path
):
    """A session id has no cross-profile continuation authority."""
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    sid = "same-desktop-session-id"
    profile_a = isolated_goal_home
    profile_b = tmp_path / "profile-b"
    profile_b.mkdir()
    manager_a = GoalManager(sid)
    manager_a.set("profile A work")
    pending_a = manager_a.prepare_continuation("continue profile A")
    assert pending_a

    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    goals._DB_CACHE.clear()
    assert GoalManager(sid).get_pending_continuation() is None

    manager_b = GoalManager(sid)
    manager_b.set("profile B work")
    pending_b = manager_b.prepare_continuation("continue profile B")
    assert pending_b and pending_b["id"] != pending_a["id"]

    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    goals._DB_CACHE.clear()
    restored_a = GoalManager(sid).get_pending_continuation()
    assert restored_a and restored_a["id"] == pending_a["id"]
    assert restored_a["prompt"] == "continue profile A"

    from tui_gateway import server
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    session_b = {"session_key": sid, "profile_home": str(profile_b)}
    claim_b = server._goal_claim_for_dispatch(session_b, pending_b)
    assert claim_b and claim_b["id"] == pending_b["id"]
    assert GoalManager(sid).get_pending_continuation()["claim_token"] is None
    assert server._goal_ack_claim(session_b, {
        "continuation_id": claim_b["id"], "continuation_claim_token": claim_b["claim_token"],
    })
    token = set_hermes_home_override(profile_b)
    try:
        assert GoalManager(sid).get_pending_continuation() is None
    finally:
        reset_hermes_home_override(token)
    assert GoalManager(sid).get_pending_continuation()["id"] == pending_a["id"]


def test_failure_recovery_is_finite_and_nonretryable_failure_pauses(isolated_goal_home):
    """Only structured retryable failures get three persisted attempts."""
    from hermes_cli.goals import GoalManager

    session_key = "desktop-goal-failure-policy"
    manager = GoalManager(session_key)
    manager.set("finish the task")
    retryable = {"failure_retryable": True, "final_response": "provider timed out"}

    for expected_attempt in range(1, 4):
        recovery = manager.record_turn_failure(retryable)
        pending = recovery["pending_continuation"]
        assert pending and pending["attempt"] == expected_attempt
        claim = manager.claim_continuation(pending["id"], now=time.time() + 61)
        assert claim
        assert manager.acknowledge_continuation(pending["id"], claim["claim_token"])

    exhausted = manager.record_turn_failure(retryable)
    assert exhausted["pending_continuation"] is None
    assert GoalManager(session_key).state.status == "paused"

    nonretryable_key = "desktop-goal-nonretryable"
    nonretryable = GoalManager(nonretryable_key)
    nonretryable.set("finish safely")
    paused = nonretryable.record_turn_failure({"final_response": "invalid credentials"})
    assert paused["pending_continuation"] is None
    assert GoalManager(nonretryable_key).state.status == "paused"


def test_pause_invalidates_persisted_intent_before_recovery_claim(isolated_goal_home):
    """A pause racing a resume cannot leave an old intent dispatchable."""
    from hermes_cli.goals import GoalManager

    session_key = "desktop-goal-pause-race"
    manager = GoalManager(session_key)
    manager.set("finish the task")
    pending = manager.prepare_continuation("continue the task")
    assert pending
    manager.pause("user paused")

    fresh = GoalManager(session_key)
    assert fresh.get_pending_continuation() is None
    assert fresh.claim_continuation(pending["id"], recover=True) is None


def test_native_failed_turn_hook_pauses_hard_failure_without_judging(isolated_goal_home, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import goals
    from tui_gateway import server

    sid = "native-hard-failure"
    manager = goals.GoalManager(sid)
    manager.set("finish safely")
    session = {"session_key": sid, "profile_home": str(isolated_goal_home),
               "agent": SimpleNamespace(_notification_config=None)}
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    monkeypatch.setattr(goals, "judge_goal", lambda *args, **kwargs: pytest.fail("must not judge error output"))
    result = {"failed": True, "completed": False, "failure_retryable": False,
              "failure_reason": "authentication_failed", "final_response": ""}
    assert server._goal_followup_after_turn(sid, session, result, "error", "") is None
    stored = goals.load_goal(sid)
    assert stored.status == "paused"
    assert stored.paused_reason == "authentication_failed"
