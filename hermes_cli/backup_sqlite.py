"""WAL-safe SQLite snapshots. Direct execution needs only the standard library.

Desktop invokes this file before stopping its backend, even when application
imports cannot load. Full and quick backups use the same SQLite copy operation.
"""
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class _SQLiteBackupTimeout(RuntimeError):
    """Raised when a SQLite snapshot remains busy past its deadline."""


def _close_quietly(conn: Optional[sqlite3.Connection]) -> None:
    if conn is not None:
        with suppress(Exception):
            conn.close()


# Desktop runs the update preflight synchronously before it stops the backend.
# A 4.4 GB state.db took about 38 s to copy and quick_check; this budget covers
# that with headroom and stays under the Desktop process limit, so a slow copy
# ends with this module's own error instead of a bare ETIMEDOUT.
PREFLIGHT_BUDGET_SECONDS = 120.0


def _pin_read_snapshot(conn: sqlite3.Connection, busy_deadline: float, timeout_seconds: float) -> None:
    """Open one read transaction on a WAL source and keep it for the whole copy.

    Without it each backup step reads the newest snapshot, and every commit by another
    connection restarts the copy from page 1, so a steady writer keeps it from finishing.
    A WAL reader does not block ordinary commits. It does keep checkpoints from passing
    its snapshot, so the -wal file grows until the copy ends, and a TRUNCATE checkpoint
    (run after VACUUM) waits out the caller's busy timeout and then gives up.
    Rollback-journal sources are left as before, because a held shared lock there would
    block the writer for the whole copy.
    """
    while True:
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                return
            conn.execute("BEGIN")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= busy_deadline:
                raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds") from exc
            time.sleep(0.1)


def _safe_copy_db(
    src: Path, dst: Path, *, timeout_seconds: float = 10.0, deadline_seconds: Optional[float] = None
) -> bool:
    """Copy a SQLite database with the backup() API (WAL-safe consistent snapshot).

    Fails closed when no consistent snapshot can be made: copying only the main file loses WAL data.
    ``deadline_seconds`` bounds the whole copy; ``timeout_seconds`` bounds one locked wait.
    """
    conn = backup_conn = None
    try:
        # sqlite3.connect() creates a missing destination with the process
        # umask, which is commonly 0022 (0644).  Snapshot databases contain
        # session and tool state, so create the inode owner-only before SQLite
        # writes its first byte.  O_NOFOLLOW also refuses a planted symlink on
        # platforms that support it.  Tighten an existing internal staging
        # file as well (NamedTemporaryFile callers already create it 0600).
        if os.name != "nt":
            open_flags = os.O_WRONLY | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            secure_fd = os.open(dst, open_flags, 0o600)
            try:
                os.fchmod(secure_fd, 0o600)
            finally:
                os.close(secure_fd)
        # timeout=0.0 disables sqlite3's implicit busy wait so the progress callback owns the
        # full locked-source deadline instead of adding the default timeout before each callback.
        # isolation_level=None leaves BEGIN to _pin_read_snapshot.
        conn = sqlite3.connect(
            f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0, isolation_level=None)
        backup_conn = sqlite3.connect(str(dst))
        started = time.monotonic()
        busy_deadline = started + max(0.0, timeout_seconds)
        _pin_read_snapshot(conn, busy_deadline, timeout_seconds)

        def _check_backup_progress(status: int, _remaining: int, _total: int) -> None:
            nonlocal busy_deadline
            now = time.monotonic()
            if deadline_seconds is not None and now - started >= deadline_seconds:
                raise _SQLiteBackupTimeout(f"copy did not finish within {deadline_seconds:g} seconds")
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if now >= busy_deadline:
                    raise _SQLiteBackupTimeout(f"database remained locked for {timeout_seconds:g} seconds")
            else:
                busy_deadline = now + max(0.0, timeout_seconds)

        conn.backup(backup_conn, pages=256, progress=_check_backup_progress, sleep=0.1)
        return True
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        # Windows won't remove the partial destination while SQLite still has it open.
        _close_quietly(backup_conn)
        backup_conn = None
        with suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    finally:
        _close_quietly(backup_conn)
        _close_quietly(conn)


def preflight_state_db(home: Path, *, budget_seconds: float = PREFLIGHT_BUDGET_SECONDS) -> dict:
    """Publish an emergency snapshot; do not prune recovery files on failure.

    The copy and its quick_check share one ``budget_seconds`` limit.
    """
    source = home / "state.db"
    if not source.exists():
        return {"path": None, "message": "state.db not found (fresh install?)"}
    prefix = "state.db.pre-update-emergency-"
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    destination = home / f"{prefix}{stamp}-{os.getpid()}.bak"
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".partial", dir=home)
    os.close(fd)
    staged = Path(name)
    deadline = time.monotonic() + budget_seconds
    try:
        if not _safe_copy_db(source, staged, deadline_seconds=budget_seconds):
            raise RuntimeError("SQLite safe copy failed; previous emergency snapshots were retained")
        connection = sqlite3.connect(str(staged))
        # A nonzero return interrupts quick_check once the shared budget is spent.
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10_000)
        try:
            try:
                result = connection.execute("PRAGMA quick_check").fetchall()
            except sqlite3.OperationalError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"SQLite snapshot check did not finish within {budget_seconds:g} seconds; "
                        "previous emergency snapshots were retained") from exc
                raise
            if result != [("ok",)]:
                raise RuntimeError(f"SQLite snapshot integrity check failed: {result}")
        finally:
            connection.close()
        size = staged.stat().st_size
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    for old in sorted(home.glob(f"{prefix}*.bak"), reverse=True)[2:]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Could not prune emergency snapshot %s: %s", old, exc)
    return {"path": str(destination), "bytes": size}


if __name__ == "__main__":
    print(json.dumps(preflight_state_db(Path(sys.argv[1]))))
