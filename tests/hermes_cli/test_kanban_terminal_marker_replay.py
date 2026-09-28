"""Dead-worker sweep: terminal-marker replay and rate-limit progress carryover.

A tool-less lane's clean exit with a fresh workspace marker lands as the real
``complete``/``block`` transition instead of a protocol violation; a marker
that fails validation falls back to the violation path. Rate-limited requeues
of long runs carry the dead run's last heartbeat note so the respawn does not
restart cold (regression-case D2).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _dead_worker(
    conn, tid: str, pid: int, rc: int, workspace: Path, *, marker: dict | None = None,
) -> str:
    """Claim ``tid`` for an already-exited worker; optionally leave a marker."""
    host = kb._claimer_id().split(":", 1)[0]
    lock = f"{host}:w{pid}"
    kb.claim_task(conn, tid, claimer=lock)
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=?, workspace_path=? "
        "WHERE id=?",
        (pid, int(time.time()) - 120, str(workspace), tid),
    )
    conn.commit()
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"the model said something\n\n{KANBAN_WORKER_EXIT_TRAILER}{rc}\n")
    if marker is not None:
        (workspace / "kanban-terminal.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )
    return lock


def _marker(lock: str, tid: str, **kw) -> dict:
    base = {
        "action": "complete",
        "task_id": tid,
        "run_token": lock,
        "summary": "finished via marker",
        "written_at": time.time(),
    }
    base.update(kw)
    return base


def _latest_event(conn, tid: str):
    return conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


def test_marker_complete_lands_done(kanban_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        lock = _dead_worker(conn, tid, 71001, 0, ws)
        (ws / "kanban-terminal.json").write_text(
            json.dumps(_marker(lock, tid)), encoding="utf-8"
        )

        crashed = kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "done"
        assert tid not in crashed
        ev = _latest_event(conn, tid)
        assert ev["kind"] == "completed"
        run = conn.execute(
            "SELECT outcome, summary FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert run["outcome"] == "completed"
        assert run["summary"] == "finished via marker"
        # Consumed exactly once.
        assert not (ws / "kanban-terminal.json").exists()


def test_marker_block_lands_blocked(kanban_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        lock = _dead_worker(conn, tid, 71002, 0, ws)
        (ws / "kanban-terminal.json").write_text(
            json.dumps(_marker(lock, tid, action="block", reason="need creds")),
            encoding="utf-8",
        )

        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert _latest_event(conn, tid)["kind"] == "blocked"


def test_foreign_marker_falls_back_to_violation(kanban_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        lock = _dead_worker(conn, tid, 71003, 0, ws)
        # Prior run's token: must not close this run.
        (ws / "kanban-terminal.json").write_text(
            json.dumps(_marker("host:1:stale", tid)), encoding="utf-8"
        )

        crashed = kbd.detect_crashed_workers(conn)

        assert tid in crashed
        assert _latest_event(conn, tid)["kind"] == "protocol_violation"
        assert kb.get_task(conn, tid).status == "ready"


def test_rate_limited_carries_last_progress(kanban_home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _dead_worker(conn, tid, 71004, kb.KANBAN_RATE_LIMIT_EXIT_CODE, ws)
        assert kbd.heartbeat_worker(conn, tid, note="halfway through the backfill")
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE task_id=?",
            (int(time.time()) - 3600, tid),
        )
        conn.commit()

        kbd.detect_crashed_workers(conn)

        ev = _latest_event(conn, tid)
        assert ev["kind"] == "rate_limited"
        payload = kb._json_dict(ev["payload"])
        assert payload.get("last_progress") == "halfway through the backfill"
        run = conn.execute(
            "SELECT error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert "halfway through the backfill" in (run["error"] or "")
