"""A private cua-driver daemon stops when the Hermes process that started it dies without cleanup.

On macOS the embedded daemon is launched through ``open``, so launchd owns it from birth. An owner ended by a signal
never reaches ``stop()``: atexit does not run on SIGKILL or SIGTERM. These tests drive a real owner process against
a fake driver, so the only thing under test is who stops the daemon.
"""

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")

REPO = Path(__file__).resolve().parents[2]

_FAKE_DRIVER = """\
#!{python}
import os, sys, time
state = {state!r}
args = sys.argv[1:]
sock = args[args.index("--socket") + 1] if "--socket" in args else ""
if args[0] == "serve":
    if os.path.exists(os.path.join(state, "delay")):
        time.sleep(1)
    open(sock, "w").close()
    open(os.path.join(state, "started"), "w").close()
    deadline = time.time() + 60
    while time.time() < deadline and not os.path.exists(os.path.join(state, "stopped")):
        time.sleep(0.05)
elif args[0] == "status":
    sys.exit(0 if os.path.exists(sock) else 1)
elif args[0] == "stop":
    with open(os.path.join(state, "stop.log"), "a") as log:
        log.write(" ".join(args) + "\\n")
    open(os.path.join(state, "stopped"), "w").close()
else:
    sys.exit(1)
"""

# Starts one embedded daemon the way a Hermes session does, prints its socket, then idles until killed. Only the
# launch wrapper and the manifest/overlay probes are replaced, so the fake driver is exec'd directly.
_OWNER = """\
import sys, time
from unittest.mock import patch
from tools.computer_use import cua_backend_daemon as daemon_mod, cua_backend_driver as driver_mod
with patch.object(daemon_mod, "_embedded_daemon_spawn_command", lambda cmd, args, **kw: [cmd, *args]), \\
     patch.object(driver_mod, "_resolve_mcp_invocation", lambda cmd, **kw: (cmd, ["mcp"])), \\
     patch.object(driver_mod, "_mcp_args_with_overlay_flag", lambda args, **kw: list(args)):
    daemon = daemon_mod._EmbeddedCuaDaemon(sys.argv[1], "unrestricted")
    if sys.argv[2] == "startup":
        start_watch = daemon._start_owner_watch
        def announce_watch(env):
            start_watch(env)
            print(daemon.socket_path, flush=True)
        daemon._start_owner_watch = announce_watch
    daemon.start()
    if sys.argv[2] != "startup":
        print(daemon.socket_path, flush=True)
    if sys.argv[2] == "clean":
        watcher = daemon._watcher
        daemon.stop()
        print(watcher.pid if watcher else "", flush=True)
    else:
        time.sleep(60)
"""


def _wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _pid_alive(pid):
    # `ps`, not os.kill(pid, 0): the watcher leaves the test's process tree once its owner exits.
    return subprocess.run(["ps", "-p", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


@pytest.fixture
def driver(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    fake = tmp_path / "cua-driver"
    fake.write_text(_FAKE_DRIVER.format(python=sys.executable, state=str(state)), encoding="utf-8")
    fake.chmod(0o755)
    owner_script = tmp_path / "owner.py"
    owner_script.write_text(textwrap.dedent(_OWNER), encoding="utf-8")
    sockets = []
    yield fake, state, owner_script, sockets
    (state / "stopped").touch()  # the fake serve exits on its own; it is outside the test's process tree
    for sock in sockets:
        with contextlib.suppress(OSError):
            os.remove(sock)


def _start_owner(fake, owner_script, mode):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO), os.environ.get("PYTHONPATH")]))}
    owner = subprocess.Popen([sys.executable, str(owner_script), str(fake), mode], cwd=REPO, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    socket_path = owner.stdout.readline().strip()
    if not socket_path:
        owner.kill()
        pytest.fail(f"owner did not start its daemon: {owner.communicate()[1][-2000:]}")
    return owner, socket_path


def test_owner_killed_by_signal_still_stops_its_daemon(driver):
    """SIGKILL to the owner's whole process group, as launchd tears down a gateway job."""
    fake, state, owner_script, sockets = driver
    owner, socket_path = _start_owner(fake, owner_script, "kill")
    sockets.append(socket_path)
    os.killpg(owner.pid, signal.SIGKILL)
    owner.wait(timeout=5)
    stop_log = state / "stop.log"
    assert _wait_for(lambda: stop_log.exists() and not os.path.exists(socket_path)), \
        "the daemon outlived its SIGKILLed owner: no `stop` reached the driver"
    assert stop_log.read_text().splitlines() == [f"stop --socket {socket_path}"]


def test_clean_stop_is_not_repeated_and_leaves_no_watcher(driver):
    fake, state, owner_script, sockets = driver
    owner, socket_path = _start_owner(fake, owner_script, "clean")
    sockets.append(socket_path)
    watcher_pid = owner.stdout.readline().strip()
    owner.wait(timeout=10)
    assert watcher_pid, "no owner watch was started"
    assert _wait_for(lambda: not _pid_alive(int(watcher_pid))), "the watcher outlived a clean stop"
    assert (state / "stop.log").read_text().splitlines() == [f"stop --socket {socket_path}"]
    assert not os.path.exists(socket_path)


def test_owner_killed_before_daemon_socket_appears_stops_later_startup(driver):
    fake, state, owner_script, sockets = driver
    (state / "delay").touch()
    owner, socket_path = _start_owner(fake, owner_script, "startup")
    sockets.append(socket_path)
    assert not os.path.exists(socket_path), "the delayed daemon became ready before the owner died"
    owner.kill()
    owner.wait(timeout=5)
    stop_log = state / "stop.log"
    assert _wait_for(lambda: (state / "started").exists() and stop_log.exists()
                     and not os.path.exists(socket_path)), \
        "the daemon outlived an owner killed during startup"
    assert stop_log.read_text().splitlines() == [f"stop --socket {socket_path}"]
