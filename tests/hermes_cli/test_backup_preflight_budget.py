"""The pre-flight budget must scale with the database, not a fixed clock cap (#124972).

A healthy 14.40 GB WAL state.db measured ~124 s to copy plus ~47 s to quick-check
on Windows, against the updater's fixed 180 s cap: the cap must not kill
progressing copies, must still kill wedged ones, and staging files left by a
hard kill must be reclaimed once their owner is provably gone.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "hermes_cli" / "backup_sqlite.py"

_LOAD_MODULE = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("backup_sqlite_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
"""

_PLAIN_RUNNER = _LOAD_MODULE + """
import json, sys
from pathlib import Path
print(json.dumps(mod.preflight_state_db(Path(sys.argv[2]))))
"""

_STALLED_COPY_RUNNER = _LOAD_MODULE + """
import sys
from pathlib import Path
real_sqlite3 = mod.sqlite3

class StalledSource(real_sqlite3.Connection):
    # A wedged copy: the backup API never returns and never reports progress.
    def backup(self, *args, **kwargs):
        import time as real_time
        while True:
            real_time.sleep(0.05)

class StalledShim:
    SQLITE_BUSY = real_sqlite3.SQLITE_BUSY
    SQLITE_LOCKED = real_sqlite3.SQLITE_LOCKED
    @staticmethod
    def connect(*args, **kwargs):
        kwargs["factory"] = StalledSource
        return real_sqlite3.connect(*args, **kwargs)

mod.sqlite3 = StalledShim
mod.preflight_state_db(Path(sys.argv[2]), stall_seconds=2.0, copy_budget_seconds=30.0)
"""

_SLOW_BUT_PROGRESSING_RUNNER = _LOAD_MODULE + """
import json, sys, threading
from pathlib import Path

class FakeTime:
    # Injected clock: every 256-page backup step "takes" 20 s of wall time, so the
    # copy runs past the old 180 s fixed cap with no real waiting. The watchdog's
    # timers run on this clock too (FakeTimer), so any cap that should expire does.
    now = 0.0
    timers = []
    @staticmethod
    def monotonic():
        return FakeTime.now
    @staticmethod
    def time():
        return FakeTime.now
    @staticmethod
    def sleep(_seconds):
        FakeTime.now += 0.05
    @staticmethod
    def advance(seconds):
        FakeTime.now += seconds
        while True:
            due = [timer for timer in FakeTime.timers if timer.alive and timer.deadline <= FakeTime.now]
            if not due:
                return
            timer = min(due, key=lambda candidate: candidate.deadline)
            timer.alive = False
            timer.callback()

class FakeTimer:
    def __init__(self, delay, callback):
        self.delay, self.callback, self.daemon, self.alive = delay, callback, False, False
    def start(self):
        self.deadline, self.alive = FakeTime.now + self.delay, True
        FakeTime.timers.append(self)
    def cancel(self):
        self.alive = False
    def is_alive(self):
        return self.alive

class ThreadingShim:
    Timer = FakeTimer
    Thread = threading.Thread
    Lock = threading.Lock

real_sqlite3 = mod.sqlite3

class ProgressingSource(real_sqlite3.Connection):
    def backup(self, target, **kwargs):
        real_progress = kwargs.get("progress")
        def wrapping(status, remaining, total):
            # Fire due timers before the ping: the watchdog lock is not reentrant.
            FakeTime.advance(20.0)
            if real_progress is not None:
                real_progress(status, remaining, total)
        kwargs["progress"] = wrapping
        return real_sqlite3.Connection.backup(self, target, **kwargs)

class ProgressingShim:
    SQLITE_BUSY = real_sqlite3.SQLITE_BUSY
    SQLITE_LOCKED = real_sqlite3.SQLITE_LOCKED
    @staticmethod
    def connect(*args, **kwargs):
        kwargs["factory"] = ProgressingSource
        return real_sqlite3.connect(*args, **kwargs)

budgets = []
real_copy_budget = mod._copy_budget_seconds
def copy_budget(source_bytes):
    budgets.append(real_copy_budget(source_bytes))
    return budgets[-1]

mod.sqlite3 = ProgressingShim
mod.time = FakeTime
mod.threading = ThreadingShim
mod._copy_budget_seconds = copy_budget
# Test-owned scale: a MiB of fixture weighs what a GiB weighs in production, so the
# size-derived budget of this ~12 MB store clears the fake copy time it must survive.
mod._GIB = 1024 ** 2
result = mod.preflight_state_db(Path(sys.argv[2]))
print(json.dumps({"result": result, "elapsed": FakeTime.now, "copy_budget": budgets[0]}))
"""

_RECLAIM_RUNNER = _LOAD_MODULE + """
import sys
from pathlib import Path
mod._reclaim_abandoned_staging(Path(sys.argv[2]))
"""

_LIVE_OWNER_RUNNER = _LOAD_MODULE + """
import sys, time as real_time
from pathlib import Path
staging = Path(sys.argv[2]) / sys.argv[3]
staging.write_bytes(b"partial")
fd = mod._acquire_owner_marker(Path(str(staging) + ".owner"))
assert fd is not None, "could not lock the owner marker"
print("locked", flush=True)
real_time.sleep(30)
"""


def _home_with_state_db(tmp_path, pages=1):
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE t (x)")
    for _ in range(pages):
        conn.execute("INSERT INTO t VALUES (zeroblob(4000))")
    conn.commit()
    conn.close()
    return home


def _run(home, runner, timeout=60):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", runner, str(SCRIPT), str(home)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def test_a_stalled_copy_is_aborted_by_the_watchdog_and_leaves_reclaimable_staging(tmp_path):
    home = _home_with_state_db(tmp_path)
    result = _run(home, _STALLED_COPY_RUNNER, timeout=30)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "no progress" in result.stderr
    partials = list(home.glob("state.db.pre-update-emergency-*.partial"))
    assert len(partials) == 1
    assert Path(str(partials[0]) + ".owner").exists()


def test_a_slow_but_progressing_copy_outlives_the_fixed_cap(tmp_path):
    home = _home_with_state_db(tmp_path, pages=3000)
    result = _run(home, _SLOW_BUT_PROGRESSING_RUNNER, timeout=60)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # The copy ran past the old fixed cap on the watchdog's own clock, and inside the
    # size-derived budget with the default stall window.
    assert 180 < payload["elapsed"] < payload["copy_budget"], payload
    assert Path(payload["result"]["path"]).exists()
    assert not list(home.glob("*.partial"))


def test_staging_left_by_a_killed_pre_flight_is_reclaimed_by_the_next_run(tmp_path):
    home = _home_with_state_db(tmp_path)
    killed = _run(home, _STALLED_COPY_RUNNER, timeout=30)
    assert killed.returncode != 0
    assert list(home.glob("*.partial"))
    recovered = _run(home, _PLAIN_RUNNER)
    assert recovered.returncode == 0, recovered.stderr
    payload = json.loads(recovered.stdout)
    assert Path(payload["path"]).exists()
    assert not list(home.glob("*.partial"))
    assert not list(home.glob("*.partial.owner"))


def test_reclaim_spared_a_live_owner_and_aged_markerless_leftovers(tmp_path):
    home = _home_with_state_db(tmp_path)
    staging_name = "state.db.pre-update-emergency-20260101T000000Z-live.partial"
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", _LIVE_OWNER_RUNNER, str(SCRIPT), str(home), staging_name],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        assert _run(home, _RECLAIM_RUNNER).returncode == 0
        assert (home / staging_name).exists()  # the owner is alive: never reclaimed
        stale = home / "state.db.pre-update-emergency-20250101T000000Z-stale.partial"
        stale.write_bytes(b"stale")
        aged = time.time() - 2 * 3600
        os.utime(stale, (aged, aged))
        fresh = home / "state.db.pre-update-emergency-20260101T000000Z-fresh.partial"
        fresh.write_bytes(b"fresh")
        assert _run(home, _RECLAIM_RUNNER).returncode == 0
        assert not stale.exists()  # marker-less and hours old: reclaimed
        assert fresh.exists()  # marker-less but young: an old-runtime copy may still be writing it
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert _run(home, _RECLAIM_RUNNER).returncode == 0
    assert not (home / staging_name).exists()
    assert not (home / f"{staging_name}.owner").exists()


def test_success_closes_owner_before_unlinking_marker(tmp_path, monkeypatch):
    from hermes_cli import backup_sqlite as mod

    home = _home_with_state_db(tmp_path)
    real_acquire = mod._acquire_owner_marker
    real_unlink = Path.unlink
    handles = {}

    def acquire(path):
        handle = real_acquire(path)
        if handle is not None:
            handles[path] = handle
        return handle

    def windows_unlink(path, *args, **kwargs):
        # Simulate Windows' refusal to delete a file with an open owner handle.
        handle = handles.get(path)
        if handle is not None and not handle.closed:
            raise PermissionError("owner marker is still open")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(mod, "_acquire_owner_marker", acquire)
    monkeypatch.setattr(Path, "unlink", windows_unlink)
    result = mod.preflight_state_db(home)
    assert Path(result["path"]).exists()
    assert not list(home.glob("*.partial.owner"))
    assert all(handle.closed for handle in handles.values())


def test_reclaim_discovers_owner_without_partial_and_spares_live_owner(tmp_path):
    home = _home_with_state_db(tmp_path)
    abandoned = home / "state.db.pre-update-emergency-orphan.partial.owner"
    abandoned.write_bytes(b" ")
    staging_name = "state.db.pre-update-emergency-live-orphan.partial"
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", _LIVE_OWNER_RUNNER,
         str(SCRIPT), str(home), staging_name],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        (home / staging_name).unlink()
        marker = home / (staging_name + ".owner")
        assert _run(home, _RECLAIM_RUNNER).returncode == 0
        assert not abandoned.exists()
        assert marker.exists()
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert _run(home, _RECLAIM_RUNNER).returncode == 0
    assert not marker.exists()


def test_phase_budgets_scale_with_the_database_size():
    from hermes_cli.backup_sqlite import _copy_budget_seconds, _quick_check_budget_seconds

    gib = 1024 ** 3
    # Never tighter than the historical fixed cap, for any size.
    assert _copy_budget_seconds(0) >= 180
    # A 14.40 GB WAL store measured ~124 s copy + ~47 s quick_check on Windows
    # (#124972); a healthy run gets at least 2x headroom.
    assert _copy_budget_seconds(14.4 * gib) >= 2 * 124
    assert _quick_check_budget_seconds(14.4 * gib) >= 2 * 47
    # Monotone in size; small stores stay at the old fixed-cap behavior.
    assert _copy_budget_seconds(14.4 * gib) >= _copy_budget_seconds(gib)
    assert _copy_budget_seconds(50 * 1024 * 1024) <= 180 + 5


def test_default_budgets_follow_the_logical_copy_and_the_finished_snapshot(tmp_path, monkeypatch):
    from hermes_cli import backup_sqlite as mod

    home = tmp_path / "home"
    home.mkdir()
    source = home / "state.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (x)")
    writer.commit()
    # A long-lived reader pins the WAL: committed growth stays out of the main file.
    reader = sqlite3.connect(source)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM t").fetchall()
    sizes = {}
    real_copy_budget = mod._copy_budget_seconds
    real_check_budget = mod._quick_check_budget_seconds
    real_quick_check = mod._quick_check

    def copy_budget(source_bytes):
        sizes["copy"] = source_bytes
        return real_copy_budget(source_bytes)

    def check_budget(source_bytes):
        sizes["check"] = source_bytes
        return real_check_budget(source_bytes)

    def quick_check(path, **kwargs):
        sizes["staged"] = path.stat().st_size
        return real_quick_check(path, **kwargs)

    monkeypatch.setattr(mod, "_copy_budget_seconds", copy_budget)
    monkeypatch.setattr(mod, "_quick_check_budget_seconds", check_budget)
    monkeypatch.setattr(mod, "_quick_check", quick_check)
    try:
        for _ in range(500):
            writer.execute("INSERT INTO t VALUES (zeroblob(4000))")
        writer.commit()
        main_bytes = source.stat().st_size
        wal_bytes = Path(str(source) + "-wal").stat().st_size
        assert wal_bytes > main_bytes  # the fixture keeps the growth in the WAL
        result = mod.preflight_state_db(home)
        assert Path(result["path"]).exists()
        assert sizes["copy"] == main_bytes + wal_bytes
        assert sizes["staged"] > main_bytes
        assert sizes["check"] == sizes["staged"]

        # Explicit budgets still bypass the size derivation.
        def unexpected(_source_bytes):
            raise AssertionError("an explicit budget was overridden")

        monkeypatch.setattr(mod, "_copy_budget_seconds", unexpected)
        monkeypatch.setattr(mod, "_quick_check_budget_seconds", unexpected)
        explicit = mod.preflight_state_db(home, copy_budget_seconds=600.0, quick_check_budget_seconds=600.0)
        assert Path(explicit["path"]).exists()
    finally:
        reader.close()
        writer.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to unlink a marker its owner holds open")
@pytest.mark.parametrize("rival", ["holds_the_marker", "reclaims_the_staging"])
def test_a_pre_flight_that_loses_its_marker_never_copies(tmp_path, monkeypatch, rival):
    import fcntl

    from hermes_cli import backup_sqlite as mod

    home = _home_with_state_db(tmp_path)
    real_flock = fcntl.flock
    real_acquire = mod._acquire_owner_marker
    rival_handles = []
    copies = []
    paused = {"done": False}

    def flock(fd, operation):
        # Pause pre-flight A after it opened its marker and before it locks; rival B runs here.
        if not paused["done"]:
            markers = [marker for marker in home.glob("*.partial.owner")
                       if os.path.samestat(os.fstat(fd), os.stat(marker))]
            if markers:
                paused["done"] = True
                if rival == "holds_the_marker":
                    rival_handles.append(real_acquire(markers[0]))
                    assert rival_handles[0] is not None
                else:
                    mod._reclaim_abandoned_staging(home)
        return real_flock(fd, operation)

    def safe_copy(*args, **kwargs):
        copies.append(args)
        return True

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(mod, "_safe_copy_db", safe_copy)
    try:
        with pytest.raises(RuntimeError, match="custody"):
            mod.preflight_state_db(home)
        assert paused["done"]
        assert copies == []
        if rival == "holds_the_marker":
            markers = list(home.glob("*.partial.owner"))
            assert len(markers) == 1  # B's marker survives
            assert os.path.samestat(os.fstat(rival_handles[0].fileno()), os.stat(markers[0]))
            assert list(home.glob("*.partial"))  # left for B's reclaim
        else:
            assert not list(home.glob("*.partial*"))
    finally:
        for handle in rival_handles:
            handle.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses to unlink a marker its owner holds open")
def test_a_marker_released_by_a_reclaimer_cannot_be_taken_over(tmp_path, monkeypatch):
    # Three parties on one staging file: pre-flight A paused before its flock,
    # reclaimer C paused after marker.exists(), and reclaimer B that reclaims A's
    # staging and then releases and removes the marker. A must refuse before any
    # copy; otherwise C later deletes A's finished partial and A publishes an
    # empty database that quick_check passes.
    import fcntl
    import threading

    from hermes_cli import backup_sqlite as mod

    home = _home_with_state_db(tmp_path)
    real_flock = fcntl.flock
    real_exists = Path.exists
    real_unlink = Path.unlink
    real_copy = mod._safe_copy_db
    real_acquire = mod._acquire_owner_marker
    events = {name: threading.Event() for name in
              ("a_paused", "a_go", "a_locked", "c_paused", "c_go", "c_done")}
    threads = {}
    copies = []
    outcome = {}

    def is_marker(path):
        return str(path).endswith(".partial.owner")

    def flock(fd, operation):
        if threading.current_thread() is threads.get("a") and not events["a_paused"].is_set():
            events["a_paused"].set()
            assert events["a_go"].wait(10)
        return real_flock(fd, operation)

    def acquire(path):
        try:
            return real_acquire(path)
        finally:
            if threading.current_thread() is threads.get("a"):
                events["a_locked"].set()  # A finished its lock and samestat check

    def exists(path, *args, **kwargs):
        found = real_exists(path, *args, **kwargs)
        if threading.current_thread() is threads.get("c") and is_marker(path) and not events["c_paused"].is_set():
            events["c_paused"].set()
            assert events["c_go"].wait(10)
        return found

    def unlink(path, *args, **kwargs):
        if threading.current_thread() is threading.main_thread() and is_marker(path) and not events["a_go"].is_set():
            # B removes the marker: let A lock and check it here, before the unlink.
            events["a_go"].set()
            assert events["a_locked"].wait(10)
        return real_unlink(path, *args, **kwargs)

    def safe_copy(src, dst, **kwargs):
        copies.append(dst)
        copied = real_copy(src, dst, **kwargs)
        assert events["c_done"].wait(10)  # C acts while A holds a finished partial
        return copied

    def run_a():
        try:
            outcome["result"] = mod.preflight_state_db(home)
        except RuntimeError as exc:
            outcome["error"] = str(exc)

    def run_c():
        try:
            mod._reclaim_abandoned_staging(home)
        finally:
            events["c_done"].set()

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(mod, "_safe_copy_db", safe_copy)
    monkeypatch.setattr(mod, "_acquire_owner_marker", acquire)
    threads["a"] = threading.Thread(target=run_a, daemon=True)
    threads["c"] = threading.Thread(target=run_c, daemon=True)
    threads["a"].start()
    assert events["a_paused"].wait(10)
    threads["c"].start()
    assert events["c_paused"].wait(10)
    mod._reclaim_abandoned_staging(home)  # B, on this thread
    events["c_go"].set()
    threads["c"].join(10)
    threads["a"].join(10)
    assert not threads["a"].is_alive() and not threads["c"].is_alive()
    assert copies == [], outcome
    assert "custody" in outcome.get("error", ""), outcome
    assert not list(home.glob("*.bak"))
    assert not list(home.glob("*.partial*"))
