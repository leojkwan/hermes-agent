"""Returned PTY children must be reaped or retained, never replayed through pipes."""

import io
import os
import sys
import time
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tools import process_registry as runtime
from tools.environments.local_execution_context import ExecutionContext
from tools.environments.local_prepared import PreparedLocalLaunch, _LaunchLifetime


pytestmark = pytest.mark.platforms("posix")


@pytest.fixture(params=["raw", "prepared"])
def launch_case(tmp_path, monkeypatch, request):
    from tools import process_registry_ownership as ownership

    registry = runtime.ProcessRegistry()
    child = SimpleNamespace(
        pid=12345, alive=True, closed=False, exitstatus=None, signalstatus=None
    )

    def close(force=False):
        child.closed, child.alive, child.exitstatus = True, False, 0

    child.close = Mock(side_effect=close)
    child.isalive = Mock(side_effect=lambda: child.alive)
    spawn = Mock(return_value=child)
    monkeypatch.setitem(
        sys.modules,
        "ptyprocess",
        SimpleNamespace(PtyProcess=SimpleNamespace(spawn=spawn)),
    )
    monkeypatch.setattr(runtime, "worker_output_log", lambda *args: None)
    monkeypatch.setattr(
        registry, "_scope_argv", lambda *args, **kwargs: ["synthetic-not-executed"]
    )
    monkeypatch.setattr(registry, "_spawn_env", lambda *args: {})
    monkeypatch.setattr(registry, "_safe_host_start_time", lambda *args: 456)
    monkeypatch.setattr(registry, "_owns_job", lambda *args: True)
    monkeypatch.setattr(registry, "_terminate_host_pid", Mock())
    monkeypatch.setattr(registry, "_write_checkpoint", Mock())
    own, release = Mock(), Mock()
    monkeypatch.setattr(ownership, "own_process_groups", own)
    monkeypatch.setattr(ownership, "release_process_groups", release)
    pipe = SimpleNamespace(
        pid=12346, stdout=io.StringIO(), stderr=None, stdin=None, poll=lambda: 0
    )
    pipe_spawn = Mock(return_value=pipe)
    monkeypatch.setattr(registry, "_spawn_job", pipe_spawn)
    sessions = []

    def track(session, *args):
        sessions.append(session)
        registry._running[session.id] = session

    monkeypatch.setattr(registry, "_track_started", track)
    launch = None
    if request.param == "prepared":
        directory = tmp_path / "launch"
        directory.mkdir()
        snapshot = directory / "state.sh"
        snapshot.write_bytes(b"")
        launch = PreparedLocalLaunch(
            "/bin/bash",
            str(tmp_path),
            MappingProxyType({}),
            b"",
            str(snapshot),
            ExecutionContext.unknown("synthetic"),
            "synthetic-not-executed",
            "synthetic-not-executed",
            None,
            time.monotonic() + 10,
            True,
            False,
            False,
            False,
            object(),
            _LaunchLifetime(str(directory)),
        )

    def run():
        return registry.spawn_local(
            "synthetic-not-executed",
            cwd=str(tmp_path),
            use_pty=True,
            prepared_launch=launch,
        )

    return SimpleNamespace(
        registry=registry,
        child=child,
        spawn=spawn,
        pipe_spawn=pipe_spawn,
        launch=launch,
        run=run,
        own=own,
        release=release,
        sessions=sessions,
    )


@pytest.mark.parametrize("launch_case", ["prepared"], indirect=True)
def test_prepared_attach_failure_terminates_only_recorded_child(
    launch_case, monkeypatch
):
    case = launch_case
    attach = Mock(side_effect=RuntimeError("synthetic attachment failure"))
    monkeypatch.setattr(PreparedLocalLaunch, "attach", attach)

    with pytest.raises(RuntimeError, match="refusing duplicate execution"):
        case.run()

    attach.assert_called_once_with(case.child)
    case.registry._terminate_host_pid.assert_called_once_with(12345, 456)
    assert case.child.pid != os.getpid()
    case.spawn.assert_called_once()
    case.pipe_spawn.assert_not_called()
    assert case.child.close.call_args_list == [call(force=True), call()]
    assert case.child.closed and not case.child.alive
    assert not case.registry._running
    assert case.launch._lifetime.closed


def test_fingerprint_failure_retains_returned_child_identity(launch_case, monkeypatch):
    case = launch_case
    fingerprint = Mock(side_effect=RuntimeError("synthetic fingerprint failure"))
    monkeypatch.setattr(case.registry, "_safe_host_start_time", fingerprint)

    with pytest.raises(RuntimeError, match="refusing duplicate execution"):
        case.run()

    fingerprint.assert_called_once_with(12345)
    case.registry._terminate_host_pid.assert_not_called()
    case.spawn.assert_called_once()
    case.pipe_spawn.assert_not_called()
    case.child.close.assert_called_once_with(force=True)
    session = next(iter(case.registry._running.values()))
    assert session._pty is case.child
    assert session.pid == 12345
    assert session.host_start_time is None
    case.release.assert_not_called()


@pytest.mark.parametrize(
    "pid,start,has_child",
    [
        (None, 456, True),
        (0, 456, True),
        (-1, 456, True),
        (True, 456, True),
        ("12345", 456, True),
        (os.getpid(), 456, True),
        (54321, 456, True),
        (12345, None, True),
        (12345, 456, False),
    ],
    ids=[
        "missing-pid", "zero-pid", "negative-pid", "boolean-pid", "string-pid",
        "current-pid", "different-child-pid", "missing-fingerprint", "missing-child",
    ],
)
def test_unusable_pty_identity_never_terminates_host_pid(
    launch_case, monkeypatch, pid, start, has_child
):
    from tools.process_registry_ownership import refuse_pty_replay

    case = launch_case
    session = case.run()
    session.pid, session.host_start_time = pid, start
    if pid == os.getpid():
        case.child.pid = pid
    if not has_child:
        session._pty = None
    session.systemd_unit = "synthetic.scope"
    stop = Mock(return_value=True)
    monkeypatch.setattr(runtime, "_stop_systemd_unit", stop)

    with pytest.raises(RuntimeError, match="refusing duplicate execution"):
        refuse_pty_replay(case.registry, session)

    case.registry._terminate_host_pid.assert_not_called()
    stop.assert_not_called()
    if has_child:
        case.child.close.assert_called_once_with(force=True)
        assert session._pty is case.child
    else:
        case.child.close.assert_not_called()
    assert case.registry._running[session.id] is session
    assert session.pid == pid and session.host_start_time == start
    assert session._owned_pgid == 12345
    assert session.systemd_unit == "synthetic.scope"
    case.release.assert_not_called()
    case.spawn.assert_called_once()
    case.pipe_spawn.assert_not_called()


@pytest.mark.parametrize("error_type", [ImportError, RuntimeError])
@pytest.mark.parametrize(
    "stage", ["ownership", "registered-ownership", "reader-registration"]
)
def test_returned_pty_is_closed_and_never_falls_back(
    launch_case, monkeypatch, error_type, stage
):
    case = launch_case

    def fail(*args, **kwargs):
        raise error_type("synthetic post-spawn failure")

    if stage == "ownership":
        monkeypatch.setattr(case.registry, "_own_pty_job", fail)
    elif stage == "registered-ownership":
        own = case.registry._own_pty_job

        def own_then_fail(*args):
            own(*args)
            fail()

        monkeypatch.setattr(case.registry, "_own_pty_job", own_then_fail)
    else:
        monkeypatch.setattr(case.registry, "_track_started", fail)
    with pytest.raises(RuntimeError, match="refusing duplicate execution"):
        case.run()
    assert case.spawn.call_count == 1
    case.pipe_spawn.assert_not_called()
    assert case.child.closed and not case.child.alive
    assert not case.registry._running
    if stage != "ownership":
        case.own.assert_called_once_with([case.child.pid])
        case.release.assert_called_once_with([case.child.pid])
    if case.launch is not None:
        assert case.launch._lifetime.closed


@pytest.mark.parametrize(
    "cleanup",
    ["terminate-refused", "close-refused", "exit-unknown", "exit-runtimeerror"],
)
def test_unproved_pty_cleanup_retains_handle_and_group(
    launch_case, monkeypatch, cleanup
):
    case = launch_case

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic reader registration failure")

    monkeypatch.setattr(case.registry, "_track_started", fail)
    if cleanup == "terminate-refused":
        case.registry._terminate_host_pid.side_effect = OSError(
            "synthetic termination refusal"
        )
    elif cleanup == "close-refused":
        case.child.close.side_effect = OSError("synthetic close refusal")
    elif cleanup == "exit-runtimeerror":
        case.child.isalive.side_effect = RuntimeError(
            "synthetic exit status unavailable"
        )
    else:
        case.child.isalive.side_effect = OSError("synthetic exit status unavailable")
    with pytest.raises(RuntimeError, match="refusing duplicate execution"):
        case.run()
    case.pipe_spawn.assert_not_called()
    assert case.spawn.call_count == 1
    assert len(case.registry._running) == 1
    session = next(iter(case.registry._running.values()))
    assert session._pty is case.child
    assert session.pid == case.child.pid
    assert session.host_start_time == 456
    assert session._owned_pgid == case.child.pid
    case.release.assert_not_called()
    if case.launch is not None:
        assert case.launch._lifetime.process is case.child
        assert case.launch._lifetime.closed == (cleanup == "terminate-refused")


def test_post_spawn_scope_refusal_retains_ownership_until_cleanup_proven(
    launch_case, monkeypatch
):
    from tools.process_registry_ownership import refuse_pty_replay

    case = launch_case

    def fail(session, *args):
        session.systemd_unit = "synthetic.scope"
        raise ImportError("synthetic reader registration failure")

    monkeypatch.setattr(case.registry, "_track_started", fail)
    stop = Mock(return_value=False)
    monkeypatch.setattr(runtime, "_stop_systemd_unit", stop)
    with pytest.raises(RuntimeError, match="refusing duplicate execution"):
        case.run()
    case.pipe_spawn.assert_not_called()
    session = next(iter(case.registry._running.values()))
    assert session._pty is case.child and session.systemd_unit == "synthetic.scope"
    assert session._owned_pgid == case.child.pid
    case.release.assert_not_called()
    stop.return_value = True
    with pytest.raises(RuntimeError, match="refusing duplicate execution"):
        refuse_pty_replay(case.registry, session)
    assert not case.registry._running and not session.systemd_unit
    case.release.assert_called_once_with([case.child.pid])


@pytest.mark.parametrize(
    "failure", ["missing-library", "spawn-import", "spawn-oserror"]
)
def test_pre_spawn_failure_permits_one_pipe_launch(launch_case, monkeypatch, failure):
    case = launch_case
    if failure == "missing-library":
        monkeypatch.setitem(sys.modules, "ptyprocess", None)
    else:
        case.spawn.side_effect = (
            ImportError("synthetic spawn failure")
            if failure == "spawn-import"
            else OSError("synthetic spawn failure")
        )
    session = case.run()
    assert session.process is case.pipe_spawn.return_value
    assert session._pty is None and session.pty_fallback
    assert case.pipe_spawn.call_count == 1
    assert case.spawn.call_count == (0 if failure == "missing-library" else 1)


@pytest.mark.parametrize("stop_succeeds", [False, True])
def test_pre_spawn_scope_requires_proven_stop_before_fallback(
    launch_case, monkeypatch, stop_succeeds
):
    case = launch_case

    def scope(session, *args, **kwargs):
        session.systemd_unit = "synthetic.scope"
        return ["synthetic-not-executed"]

    monkeypatch.setattr(case.registry, "_scope_argv", scope)
    stop = Mock(return_value=stop_succeeds)
    monkeypatch.setattr(runtime, "_stop_systemd_unit", stop)
    case.spawn.side_effect = ImportError("synthetic pre-spawn failure")
    if stop_succeeds:
        case.run()
        assert case.pipe_spawn.call_count == 1
    else:
        with pytest.raises(RuntimeError, match="could not be reaped"):
            case.run()
        case.pipe_spawn.assert_not_called()
    stop.assert_called_once_with("synthetic.scope")
