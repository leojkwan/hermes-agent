"""_safe_copy_db against a live source: busy deadline, a committing writer, and the total budget."""
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.waiting import wait_until


def test_aborts_when_source_remains_busy_past_deadline(tmp_path, monkeypatch):
    from hermes_cli import backup as backup_mod

    src = tmp_path / "locked.db"
    dst = tmp_path / "copy.db"
    src.touch()
    dst.write_bytes(b"partial")

    clock = iter((100.0, 100.5, 101.1))

    class FakeSourceConnection:
        in_transaction = False

        def execute(sql):
            assert sql == "PRAGMA journal_mode"  # a rollback-journal source is not pinned
            return type("Cursor", (), {"fetchone": lambda _self: ("delete",)})()

        def backup(_destination, *, pages, progress, sleep):
            assert pages > 0
            assert sleep > 0
            progress(sqlite3.SQLITE_BUSY, 0, 1)
            progress(sqlite3.SQLITE_BUSY, 0, 1)

        def close(self):
            pass

    destination_closed = []

    class FakeDestinationConnection:
        def close(self):
            destination_closed.append(True)

    connections = iter((FakeSourceConnection(), FakeDestinationConnection()))
    real_unlink = Path.unlink

    def assert_closed_before_unlink(path, *args, **kwargs):
        assert destination_closed
        return real_unlink(path, *args, **kwargs)

    connect_calls = []

    def fake_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return next(connections)

    monkeypatch.setattr(backup_mod.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(backup_mod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(Path, "unlink", assert_closed_before_unlink)

    assert backup_mod._safe_copy_db(src, dst, timeout_seconds=1.0) is False
    assert connect_calls[0][1]["timeout"] == 0.0
    assert not dst.exists()


def _wal_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (body TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", (("x" * 400,) for _ in range(rows)))
    conn.commit()
    conn.close()


def test_copy_finishes_while_another_connection_keeps_committing(tmp_path):
    """An unpinned copy restarts on every commit by another connection and never ends."""
    from hermes_cli.backup import _safe_copy_db

    src = tmp_path / "state.db"
    dst = tmp_path / "copy.db"
    _wal_db(src, 40_000)  # about 4,000 pages: many 256-page backup steps
    stop = threading.Event()
    commits = []

    def writer():
        conn = sqlite3.connect(str(src), timeout=5.0)
        while not stop.is_set():
            conn.execute("INSERT INTO t VALUES ('w')")
            conn.commit()
            commits.append(1)
            stop.wait(0.001)  # a steady writer: one commit about every millisecond
        conn.close()

    copied = threading.Event()
    result = []

    def copy():
        result.append(_safe_copy_db(src, dst))
        copied.set()

    writing = threading.Thread(target=writer)
    writing.start()
    wait_until(lambda: commits, what="the writer's first commit")
    copying = threading.Thread(target=copy)
    copying.start()
    try:
        finished_while_writing = copied.wait(20.0)
        commits_during_copy = len(commits)
    finally:
        stop.set()
        writing.join()
        copying.join()
    assert finished_while_writing, "the copy did not finish while the writer kept committing"
    assert commits_during_copy > 1
    assert result == [True]
    conn = sqlite3.connect(str(dst))
    try:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] >= 40_000
    finally:
        conn.close()


def test_copy_stops_at_its_total_deadline(tmp_path):
    from hermes_cli.backup import _safe_copy_db

    src = tmp_path / "state.db"
    dst = tmp_path / "copy.db"
    _wal_db(src, 40_000)
    assert _safe_copy_db(src, dst, deadline_seconds=0.0) is False
    assert not dst.exists()


def test_preflight_check_stops_at_the_shared_budget(tmp_path, monkeypatch):
    from hermes_cli import backup_sqlite

    _wal_db(tmp_path / "state.db", 40_000)
    real_copy = backup_sqlite._safe_copy_db
    monkeypatch.setattr(backup_sqlite, "_safe_copy_db", lambda src, dst, **_kw: real_copy(src, dst))
    with pytest.raises(RuntimeError, match="did not finish within 0 seconds"):
        backup_sqlite.preflight_state_db(tmp_path, budget_seconds=0.0)
    assert not list(tmp_path.glob("*.bak"))
    assert not list(tmp_path.glob("*.partial"))
