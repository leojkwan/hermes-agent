"""FBL-370 prepared launch producer/consumer contracts using only synthetic state."""

import json
import os
import shlex
import shutil
import threading
import time
from pathlib import Path

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments import local
from tools.process_registry import ProcessRegistry
from tests.waiting import wait_until


pytestmark = pytest.mark.platforms("posix")


@pytest.fixture(params=["/bin/bash", "/opt/homebrew/bin/bash"])
def backend(tmp_path, monkeypatch, request):
    if not Path(request.param).is_file():
        pytest.skip("Bash build unavailable on this host")
    home = tmp_path / "home"
    home.mkdir()
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(local, "_find_bash", lambda: request.param)
    original = local.LocalEnvironment._run_bash
    environments = []

    def build(initial=""):
        def isolated(self, script, *, login=False, **kwargs):
            return original(
                self, (initial + "\n" if login else "") + script, login=False, **kwargs
            )

        monkeypatch.setattr(local.LocalEnvironment, "_run_bash", isolated)
        env = local.LocalEnvironment(cwd=str(tmp_path), timeout=5)
        env.env_type = "local"
        environments.append(env)
        assert env._snapshot_ready
        return env

    yield build
    for env in environments:
        env.cleanup()


def test_prepared_binds_exact_snapshot_cwd_env_and_bash(backend, tmp_path, monkeypatch):
    env = backend("export SAVED=original; saved_fn () { printf function; }")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("SYNTHETIC_SCOPE", "first")
    command = 'printf "%s|%s|%s|" "$SAVED" "$SYNTHETIC_SCOPE" "$PWD"; saved_fn'
    launch = env.prepare_local_launch(command, cwd=str(tmp_path), timeout=5)
    path = Path(launch.snapshot_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert launch.bash == local._find_bash() and os.path.isabs(launch.bash)
    with pytest.raises(TypeError):
        launch.spawn_env["SYNTHETIC_SCOPE"] = "mutated"
    Path(env._snapshot_path).write_text("export SAVED=replacement\n")
    monkeypatch.setenv("SYNTHETIC_SCOPE", "second")
    env.cwd = "/"
    result = env.execute_prepared(launch)
    assert result["returncode"] == 0
    assert result["output"] == f"original|first|{tmp_path}|function"
    assert not path.exists()
    with pytest.raises(RuntimeError, match="already consumed"):
        env.execute_prepared(launch)
    assert not any(
        value in repr(launch) for value in ("SAVED", "original", "first", str(tmp_path))
    )


def test_preparation_recovers_missing_snapshot_before_admission(backend, tmp_path):
    env = backend("export SAVED=rebuilt")
    Path(env._snapshot_path).unlink()
    counter = tmp_path / "counter"
    launch = env.prepare_local_launch(
        'printf "%s" "$SAVED"; printf once >> ' + shlex.quote(str(counter)), timeout=5
    )
    assert launch.context.node_options.status != "unknown"
    result = env.execute_prepared(launch)
    assert result["output"] == "rebuilt" and result["environment_recreated"]
    assert counter.read_text() == "once"
    from tools.terminal_tool_result import finalize_foreground_result

    rendered = json.loads(
        finalize_foreground_result(
            command=launch.command,
            result=result,
            env=env,
            env_type="local",
            effective_task_id="synthetic",
            task_id="synthetic",
            session_id=None,
            session_key="synthetic",
            workdir=None,
            command_cwd=str(tmp_path),
            approval_note=None,
        )
    )
    assert (
        "saved local Bash environment was recreated"
        in rendered["environment_recreated"]
    )
    assert "container" not in rendered["environment_recreated"]
    assert not env._prefer_nonlogin


def test_failed_recovery_is_bounded_and_never_runs_command(
    backend, tmp_path, monkeypatch
):
    env = backend()
    Path(env._snapshot_path).unlink()
    started = time.monotonic()
    release = threading.Event()

    def stalled(*args, **kwargs):
        release.wait()
        raise OSError("SYNTHETIC_PRIVATE_FAILURE")

    monkeypatch.setattr(env, "_recover_local_snapshot", stalled)
    with pytest.raises(RuntimeError, match="not started") as error:
        env.prepare_local_launch("printf should-not-run", timeout=0.1)
    assert time.monotonic() - started < 2
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
    release.set()
    assert not env._prefer_nonlogin


def test_background_reads_prepared_state_without_publishing(backend, tmp_path):
    env = backend("export SAVED=foreground")
    before = Path(env._snapshot_path).read_bytes()
    launch = env.prepare_local_launch(
        'printf "%s" "$SAVED"; export SAVED=background', background=True, timeout=5
    )
    private = Path(launch.snapshot_path)
    registry = ProcessRegistry()
    session = registry.spawn_local(launch.command, prepared_launch=launch)
    result = registry.wait(session.id, timeout=5)
    assert result["exit_code"] == 0 and "foreground" in result["output"]
    assert Path(env._snapshot_path).read_bytes() == before
    assert wait_until(lambda: not private.exists(), timeout=2)


def test_yielded_publisher_keeps_pid_and_final_publication(backend, tmp_path):
    env = backend("export SAVED=old")
    release = tmp_path / "release"
    launch = env.prepare_local_launch(
        "while [ ! -f "
        + shlex.quote(str(release))
        + " ]; do /bin/sleep 0.02; done; export SAVED=final",
        timeout=5,
    )
    registry = ProcessRegistry()
    adopted = []

    def handoff(proc, output):
        session = registry.adopt_local(proc, command=launch.command, cwd=launch.cwd)
        adopted.append((session, proc.pid))
        return {"output": output, "returncode": None, "yielded_session_id": session.id}

    from tools.interrupt import request_yield

    request_yield(threading.get_ident())
    result = env.execute_prepared(launch, yield_handler=handoff)
    assert result["yielded_session_id"] == adopted[0][0].id
    assert adopted[0][0].pid == adopted[0][1]
    second = env.prepare_local_launch("export SAVED=wrong", timeout=5)
    result = env.execute_prepared(second)
    assert result["state_not_persisted"]
    release.touch()
    assert registry.wait(adopted[0][0].id, timeout=5)["exit_code"] == 0
    third = env.prepare_local_launch('printf "%s" "$SAVED"', timeout=5)
    assert env.execute_prepared(third)["output"] == "final"


@pytest.mark.parametrize("background", [True, False])
def test_abandoned_or_timed_out_launch_cleans_private_files(
    backend, tmp_path, background
):
    env = backend()
    launch = env.prepare_local_launch(
        "/bin/sleep 5", background=background, timeout=0.1
    )
    private = Path(launch.snapshot_path).parent
    if background:
        launch.close()
    else:
        assert env.execute_prepared(launch)["hermes_timed_out"]
    assert not private.exists()


@pytest.mark.parametrize("background", [False, True])
def test_plugin_credential_sentinel_is_stripped_by_prepared_factory(
    backend, monkeypatch, background
):
    env = backend()
    monkeypatch.setenv("SYNTHETIC_PLUGIN_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setattr(
        local,
        "_plugin_terminal_env_strip_keys",
        lambda: frozenset({"SYNTHETIC_PLUGIN_KEY"}),
    )
    launch = env.prepare_local_launch(
        "printf harmless", background=background, timeout=5
    )
    try:
        assert "SYNTHETIC_PLUGIN_KEY" not in launch.spawn_env
        assert "SYNTHETIC_SECRET" not in repr(launch)
    finally:
        launch.close()


@pytest.mark.parametrize("mode", ["foreground", "explicit", "trailing", "promoted"])
@pytest.mark.parametrize("program", ["node safe.cjs", "npm test"])
@pytest.mark.parametrize("preload", [False, True])
def test_terminal_modes_scan_and_spawn_same_synthetic_graph(
    backend, tmp_path, monkeypatch, mode, program, preload
):
    from tests.cron.lifecycle_context_fixtures import npm_context
    from tools import terminal_tool as terminal
    from tools import process_registry as processes

    node = Path("/opt/homebrew/bin/node")
    if not node.is_file():
        pytest.skip("Fixture smoke requires an existing Node binary; no installation")
    facts = npm_context(tmp_path)
    binary = Path(facts.path.value) / "node"
    binary.unlink()
    binary.symlink_to(node)
    cli = tmp_path / ".fixture-tools/lib/node_modules/npm/bin/npm-cli.js"
    cli.write_text(
        "#!/usr/bin/env node\nrequire('child_process').execFileSync(process.execPath, ['safe.cjs'], {stdio:'inherit'});\n"
    )
    cli.chmod(0o700)
    counter = tmp_path / "runs"
    (tmp_path / "safe.cjs").write_text(
        "require('fs').appendFileSync('runs', 'once'); console.log('fixture-ok');\n"
    )
    (tmp_path / "stop.cjs").write_text(
        "if (false) require('child_process').execSync('hermes gateway restart');\n"
    )
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node safe.cjs"}}')
    initial = (
        "export HOME="
        + shlex.quote(facts.home.value)
        + " PATH="
        + shlex.quote(facts.path.value + ":/usr/bin:/bin")
    )
    if preload:
        initial += " NODE_OPTIONS=--require=./stop.cjs"
    env = backend(initial)
    registry = ProcessRegistry()
    monkeypatch.setattr(processes, "process_registry", registry)
    monkeypatch.setattr(processes, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(
        terminal,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "timeout": 5,
            "cwd": str(tmp_path),
            "host_cwd": None,
            "modal_mode": "auto",
            "docker_image": "",
            "modal_image": "",
            "daytona_image": "",
        },
    )
    monkeypatch.setattr(terminal, "_acquire_env", lambda *args: env)
    monkeypatch.setattr(terminal, "get_session_cwd", lambda *args: None)
    result = json.loads(
        terminal.terminal_tool(
            program + (" &" if mode == "trailing" else ""),
            background=mode == "explicit",
            timeout=601 if mode == "promoted" else 5,
        )
    )
    if preload:
        assert result["error"] and not counter.exists()
    elif mode == "foreground":
        assert result["exit_code"] == 0 and "fixture-ok" in result["output"]
    else:
        assert result.get("session_id"), result
        finished = registry.wait(result["session_id"], timeout=5)
        assert finished["exit_code"] == 0 and "fixture-ok" in finished["output"]
    if not preload:
        assert counter.read_text() == "once"
    assert not list(tmp_path.glob("hermes-launch-*"))


def test_prepared_pty_fallback_once_uses_same_inputs(backend, tmp_path, monkeypatch):
    env = backend("export SAVED=frozen")
    launch = env.prepare_local_launch(
        'printf "%s" "$SAVED"', background=True, timeout=5
    )
    registry = ProcessRegistry()
    calls = []

    def unavailable(session, script, spawn, prepared_launch=None):
        calls.append((script, spawn, prepared_launch))
        Path(env._snapshot_path).write_text("export SAVED=changed\n")
        env.env["SAVED"] = "changed"
        raise ImportError("fixture has no PTY")

    monkeypatch.setattr(registry, "_spawn_local_pty", unavailable)
    session = registry.spawn_local(launch.command, prepared_launch=launch, use_pty=True)
    result = registry.wait(session.id, timeout=5)
    assert len(calls) == 1 and calls[0] == (launch.script, launch.spawn_env, launch)
    assert result["exit_code"] == 0 and "frozen" in result["output"]
    assert not Path(launch.snapshot_path).exists()


def test_spawn_failure_cleans_without_retry_or_private_diagnostic(backend, monkeypatch):
    env = backend()
    launch = env.prepare_local_launch("printf harmless", timeout=5)

    def fail(*args, **kwargs):
        raise OSError("synthetic spawn failed")

    monkeypatch.setattr(env, "_spawn_prepared", fail)
    with pytest.raises(OSError):
        env.execute_prepared(launch)
    assert not Path(launch.snapshot_path).exists()


@pytest.mark.parametrize("cleanup", ["missing", "denied"])
def test_foreground_cleanup_releases_publisher_and_keeps_result(
    backend, tmp_path, monkeypatch, cleanup
):
    from tools.environments import local_prepared

    env = backend()
    ready, release = tmp_path / "ready", tmp_path / "release"
    remove_directory = shutil.rmtree
    launch = env.prepare_local_launch(
        f": > {shlex.quote(str(ready))}; "
        f"while [ ! -f {shlex.quote(str(release))} ]; do /bin/sleep 0.02; done; "
        "printf original-result; exit 7"
    )
    results = []

    def execute():
        try:
            results.append(env.execute_prepared(launch))
        except Exception as error:
            results.append(error)

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert wait_until(ready.exists, timeout=3)
        if cleanup == "missing":
            shutil.rmtree(Path(launch.snapshot_path).parent)
        else:

            def denied(*args, **kwargs):
                raise PermissionError("synthetic cleanup denied")

            monkeypatch.setattr(local_prepared.shutil, "rmtree", denied)
    finally:
        release.touch()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert results[0]["returncode"] == 7
    assert results[0]["output"] == "original-result"
    assert launch._lifetime.closed
    monkeypatch.setattr(local_prepared.shutil, "rmtree", remove_directory)
    exported = env.prepare_local_launch("export CLEANUP_SURVIVED=present")
    assert exported.publishes
    assert env.execute_prepared(exported)["returncode"] == 0
    read = env.prepare_local_launch('printf "%s" "$CLEANUP_SURVIVED"')
    assert env.execute_prepared(read)["output"] == "present"


@pytest.mark.parametrize("background", [False, True])
def test_cleanup_failure_preserves_original_spawn_error(
    backend, monkeypatch, background
):
    from tools.environments import local_prepared

    env = backend()
    launch = env.prepare_local_launch("synthetic-not-executed", background=background)
    original = OSError("synthetic spawn refusal")

    def fail(*args, **kwargs):
        raise original

    def denied(*args, **kwargs):
        raise PermissionError("synthetic cleanup denied")

    monkeypatch.setattr(local_prepared.shutil, "rmtree", denied)
    if background:
        registry = ProcessRegistry()
        monkeypatch.setattr(registry, "_spawn_job", fail)
        execute = lambda: registry.spawn_local(launch.command, prepared_launch=launch)
    else:
        monkeypatch.setattr(env, "_spawn_prepared", fail)
        execute = lambda: env.execute_prepared(launch)
    with pytest.raises(OSError) as caught:
        execute()
    assert caught.value is original
    assert launch._lifetime.closed


def test_prepare_cleanup_failure_preserves_original_error(backend, monkeypatch):
    from tools.environments import local_prepared

    env = backend()
    original = ValueError("synthetic wrapper refusal")

    def fail(*args, **kwargs):
        raise original

    def denied(*args, **kwargs):
        raise PermissionError("synthetic cleanup denied")

    monkeypatch.setattr(local_prepared, "_wrap_command_script", fail)
    monkeypatch.setattr(local_prepared.shutil, "rmtree", denied)
    with pytest.raises(ValueError) as caught:
        local_prepared._prepare(
            env, "synthetic-not-executed", "", time.monotonic() + 5, False
        )
    assert caught.value is original


@pytest.mark.parametrize("cleanup", ["missing", "denied"])
def test_finished_background_cleanup_still_closes_all_streams(
    backend, monkeypatch, cleanup
):
    import io
    from types import SimpleNamespace
    from tools.environments import local_prepared

    env = backend()
    launch = env.prepare_local_launch("synthetic-not-executed", background=True)
    streams = [io.StringIO() for _ in range(3)]
    proc = SimpleNamespace(
        poll=lambda: 0, stdout=streams[0], stderr=streams[1], stdin=streams[2]
    )
    launch.attach(proc)
    if cleanup == "missing":
        shutil.rmtree(Path(launch.snapshot_path).parent)
    else:

        def denied(*args, **kwargs):
            raise PermissionError("synthetic cleanup denied")

        monkeypatch.setattr(local_prepared.shutil, "rmtree", denied)
    registry = ProcessRegistry()
    session = registry._new_session(launch.command, "", "", "", env.cwd)
    session.process = proc
    registry._release_finished_handles(session)
    registry._release_finished_handles(session)
    assert launch._lifetime.closed
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("background", [False, True])
def test_prepared_profile_strips_launch_residue_and_keeps_target(
    backend, tmp_path, monkeypatch, background
):
    from agent import secret_scope
    import hermes_constants
    from hermes_cli import env_loader

    env = backend()
    target = tmp_path / "served-profile"
    target.mkdir()
    monkeypatch.setenv("SYNTHETIC_LAUNCH_SETTING", "launch-only")
    monkeypatch.setattr(hermes_constants, "get_hermes_home_override", lambda: target)
    monkeypatch.setattr(
        hermes_constants,
        "get_routing_process_hermes_home",
        lambda: tmp_path / "launch-profile",
    )
    monkeypatch.setattr(local, "_is_routed_home", lambda *args: True)
    monkeypatch.setattr(
        secret_scope,
        "load_env_file",
        lambda *args: {"SYNTHETIC_LAUNCH_SETTING": "launch-only"},
    )
    monkeypatch.setattr(env_loader, "launch_dotenv_keys", lambda: frozenset())
    monkeypatch.setattr(env_loader, "managed_dotenv_keys", lambda: frozenset())
    monkeypatch.setattr(env_loader, "source_supplied_names", lambda: frozenset())
    env.env["SYNTHETIC_TARGET_SETTING"] = "served"
    launch = env.prepare_local_launch(
        'printf "%s|%s" "$SYNTHETIC_TARGET_SETTING" "${SYNTHETIC_LAUNCH_SETTING-unset}"',
        background=background,
    )
    assert "SYNTHETIC_LAUNCH_SETTING" not in launch.spawn_env
    assert launch.spawn_env["SYNTHETIC_TARGET_SETTING"] == "served"
    if background:
        registry = ProcessRegistry()
        session = registry.spawn_local(launch.command, prepared_launch=launch)
        result = registry.wait(session.id, timeout=5)
        assert "served|unset" in result["output"]
    else:
        assert env.execute_prepared(launch)["output"] == "served|unset"


@pytest.mark.parametrize("name", ["SHELLOPTS", "BASHOPTS"])
def test_inherited_assignment_modes_invalidate_prepared_facts(
    backend, monkeypatch, name
):
    env = backend()
    monkeypatch.setenv(name, "keyword")
    launch = env.prepare_local_launch("node -e 1")
    try:
        assert scan_gateway_lifecycle(launch.command, execution_context=launch.context)[
            0
        ]
    finally:
        launch.close()


def test_raw_internal_command_cannot_overwrite_live_prepared_publisher(
    backend, tmp_path
):
    env = backend("export SAVED=original")
    launch = env.prepare_local_launch("export SAVED=prepared", timeout=5)
    result = env.execute("export SAVED=raw", timeout=5)
    assert result["state_not_persisted"]
    env.execute_prepared(launch)
    assert env.execute('printf "%s" "$SAVED"')["output"] == "prepared"


def test_restore_cleanup_failure_refuses_before_user_command(backend, tmp_path):
    env = backend("rm () { return 1; }")
    counter = tmp_path / "counter"
    launch = env.prepare_local_launch("printf once >> " + shlex.quote(str(counter)))
    result = env.execute_prepared(launch)
    assert result["returncode"] == 126 and not counter.exists()
    assert "command was not started" in result["output"]
    assert not Path(launch.snapshot_path).parent.exists()
    assert "__HERMES_RESTORE" not in result["output"]


def test_private_copy_failure_cleans_before_any_execution(
    backend, tmp_path, monkeypatch
):
    from tools.environments import local_prepared

    env = backend()

    def unavailable(*args, **kwargs):
        raise OSError("SYNTHETIC_PRIVATE_COPY_FAILURE")

    monkeypatch.setattr(local_prepared, "open", unavailable, raising=False)
    with pytest.raises(RuntimeError, match="command was not started") as error:
        env.prepare_local_launch("printf harmless")
    assert "SYNTHETIC_PRIVATE_COPY_FAILURE" not in str(error.value)
    assert not list(tmp_path.glob("hermes-launch-*"))


def test_real_prepared_pty_uses_saved_bash_state(backend):
    pytest.importorskip("ptyprocess")
    env = backend("export SAVED=pty-frozen")
    launch = env.prepare_local_launch('printf "%s" "$SAVED"', background=True)
    registry = ProcessRegistry()
    session = registry.spawn_local(launch.command, use_pty=True, prepared_launch=launch)
    result = registry.wait(session.id, timeout=5)
    assert result["exit_code"] == 0 and "pty-frozen" in result["output"]
    assert wait_until(lambda: not Path(launch.snapshot_path).exists(), timeout=2)


def test_killed_prepared_background_cleans_private_copy(backend):
    env = backend()
    launch = env.prepare_local_launch("/bin/sleep 10", background=True)
    registry = ProcessRegistry()
    session = registry.spawn_local(launch.command, prepared_launch=launch)
    registry.kill_process(session.id)
    assert wait_until(lambda: not Path(launch.snapshot_path).exists(), timeout=3)


def test_prepared_restore_retry_keeps_saved_state_without_reset(backend):
    env = backend("export SAVED=retained")
    env._snapshot_ready = False
    env._snapshot_recovery_pending = "unavailable"
    launch = env.prepare_local_launch('printf "%s" "$SAVED"')
    result = env.execute_prepared(launch)
    assert result["output"] == "retained"
    assert not result.get("environment_recreated")
