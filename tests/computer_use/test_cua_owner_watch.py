"""Real-process regressions for the private daemon's escaped lifetime.

Adapted from Leo Kwan's e41c50e042773e76f514e0869134de7036824c01.
The fake launch wrapper forks a daemon into its own session, like LaunchServices;
killing the owner's entire group must therefore leave the daemon alive on base.
"""

import contextlib
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")
REPO = Path(__file__).resolve().parents[2]

_FAKE_DRIVER = r'''\
#!{python}
import os, sys, time
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
sock = Path(args[args.index("--socket") + 1])
if args[0] == "serve":
    pid = os.fork()
    if pid == 0:
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.close(null)
        (state / "daemon.pid").write_text(str(os.getpid()))
        (state / "daemon.group").write_text(str(os.getpgrp()))
        # Death during launch must also be covered before the socket exists.
        if (state / "delay").exists():
            time.sleep(1)
        sock.touch()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (state / "stopped").exists():
            time.sleep(0.02)
        sock.unlink(missing_ok=True)
        (state / "exited").touch()
        os._exit(0)
    while not (state / "exited").exists():
        time.sleep(0.02)
elif args[0] == "status":
    sys.exit(0 if sock.exists() else 1)
elif args[0] == "stop":
    with (state / "stop.log").open("a") as log:
        log.write(" ".join(args) + "\n")
    (state / "stopped").touch()
else:
    sys.exit(1)
'''

_OWNER = '''\
import sys, time
from pathlib import Path
from unittest.mock import patch
from tools.computer_use import cua_backend_daemon as dm, cua_backend_driver as driver
fake, mode, state = sys.argv[1], sys.argv[2], Path(sys.argv[3])
real_popen = dm.subprocess.Popen
def popen(command, **kwargs):
    watch = len(command) > 1 and command[1] == "-I"
    if watch and mode == "refuse-spawn":
        raise OSError("injected owner-watch spawn refusal")
    if watch and mode == "refuse-exec":
        command = [command[0], "-I", str(state / "missing-watch.py")]
    process = real_popen(command, **kwargs)
    if watch:
        (state / "watcher.pid").write_text(str(process.pid))
    return process
with patch.object(dm, "_embedded_daemon_spawn_command", lambda cmd, args, **kw: [cmd, *args]), \\
     patch.object(driver, "_resolve_mcp_invocation", lambda cmd, **kw: (cmd, ["mcp"])), \\
     patch.object(driver, "_mcp_args_with_overlay_flag", lambda args, **kw: list(args)), \\
     patch.object(dm.subprocess, "Popen", popen):
    daemon = dm._EmbeddedCuaDaemon(fake, "unrestricted")
    (state / "socket").write_text(daemon.socket_path)
    try:
        daemon.start()
    except (OSError, RuntimeError):
        if mode.startswith("refuse-"):
            (state / "refused").touch()
            daemon.stop()
            sys.exit(0)
        raise
    print("ready", flush=True)
    if mode == "clean":
        daemon.stop()
        daemon.stop()
    else:
        time.sleep(30)
'''


def _wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _pid_alive(pid):
    return subprocess.run(["ps", "-p", str(pid)], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=2).returncode == 0


@pytest.fixture
def driver(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    fake = tmp_path / "cua-driver"
    fake.write_text(_FAKE_DRIVER.lstrip("\\\n").format(python=sys.executable, state=str(state)))
    fake.chmod(0o755)
    script = tmp_path / "owner.py"
    script.write_text(textwrap.dedent(_OWNER))
    owners = []
    yield fake, state, script, owners
    # Only processes created by this fixture are stopped, including failed-base orphans.
    for owner in owners:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
        owner.communicate(timeout=5)
    (state / "stopped").touch()
    for name in ("daemon.pid", "watcher.pid"):
        path = state / name
        if path.exists():
            pid = int(path.read_text())
            assert _wait_for(lambda: not _pid_alive(pid), seconds=18), f"test {name} was not reaped"
    path = state / "socket"
    if path.exists():
        with contextlib.suppress(FileNotFoundError):
            os.remove(path.read_text())


def _start_owner(driver, mode, *, ready=True):
    fake, state, script, owners = driver
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    owner = subprocess.Popen([sys.executable, str(script), str(fake), mode, str(state)],
                             cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    owners.append(owner)
    if ready:
        assert select.select([owner.stdout], [], [], 10)[0], "owner startup did not finish"
        assert owner.stdout.readline().strip() == "ready", "owner failed to start"
    return owner


@pytest.mark.parametrize("during_startup", [False, True])
def test_owner_killed_by_signal_still_stops_its_daemon(driver, during_startup):
    _, state, _, _ = driver
    if during_startup:
        (state / "delay").touch()
    owner = _start_owner(driver, "kill", ready=not during_startup)
    assert _wait_for(lambda: (state / "daemon.group").exists())
    assert int((state / "daemon.group").read_text()) != owner.pid
    assert not (state / "exited").exists(), "fake daemon must be alive before group kill"
    os.killpg(owner.pid, signal.SIGKILL)
    owner.wait(timeout=5)
    socket_path = (state / "socket").read_text()
    assert _wait_for(lambda: (state / "stop.log").exists() and (state / "exited").exists()), \
        "the detached daemon outlived its SIGKILLed owner"
    assert (state / "stop.log").read_text().splitlines() == [f"stop --socket {socket_path}"]
    assert not os.path.exists(socket_path)


def test_clean_stop_is_not_repeated_and_leaves_no_watcher(driver):
    _, state, _, _ = driver
    owner = _start_owner(driver, "clean")
    assert owner.wait(timeout=10) == 0
    assert (state / "watcher.pid").exists(), "no owner watch was started"
    assert not _pid_alive(int((state / "watcher.pid").read_text())), "watcher outlived clean stop"
    socket_path = (state / "socket").read_text()
    assert (state / "stop.log").read_text().splitlines() == [f"stop --socket {socket_path}"]
    assert (state / "exited").exists()
    assert not os.path.exists(socket_path)


@pytest.mark.parametrize("mode", ["refuse-spawn", "refuse-exec"])
def test_guard_start_refusal_leaves_no_unguarded_daemon(driver, mode):
    _, state, _, _ = driver
    owner = _start_owner(driver, mode, ready=False)
    assert owner.wait(timeout=10) == 0
    assert (state / "refused").exists(), "guard refusal allowed daemon startup"
    assert not (state / "daemon.pid").exists(), "daemon launched without a working guard"
    assert not os.path.exists((state / "socket").read_text())
    if (state / "watcher.pid").exists():
        assert not _pid_alive(int((state / "watcher.pid").read_text()))
