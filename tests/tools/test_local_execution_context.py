"""FBL-370 foreground facts through the production local factory and isolated Bash."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments import local
from tools.environments.base_session_env import _export_dump_excluding_session_vars
from tools.environments.local_execution_context import MAX_SNAPSHOT_BYTES
from tools.terminal_tool_backends import _create_environment


@pytest.fixture(params=["/bin/bash", "/opt/homebrew/bin/bash"])
def bash(request):
    if not Path(request.param).is_file():
        pytest.skip("Bash build unavailable on this host")
    return request.param


@pytest.fixture
def backend(tmp_path, monkeypatch, bash):
    task_home = tmp_path / "home"
    task_home.mkdir()
    minimal = {
        "HOME": str(task_home),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes"),
    }
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in minimal.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(Path, "home", lambda: task_home)
    monkeypatch.setattr(local, "_find_bash", lambda: bash)
    original = local.LocalEnvironment._run_bash
    environments = []

    def build(initial="", persistent=True):
        def isolated(self, script, *, login=False, **kwargs):
            # Exercise the real wrapper/spawn map without any host login/init files.
            return original(
                self, (initial + "\n" if login else "") + script, login=False, **kwargs
            )

        monkeypatch.setattr(local.LocalEnvironment, "_run_bash", isolated)
        env = _create_environment(
            "local",
            image="",
            cwd=str(tmp_path),
            timeout=10,
            local_config={"persistent": persistent},
        )
        environments.append(env)
        assert env.env_type == "local" and env._snapshot_ready
        return env

    yield build
    for env in environments:
        env.cleanup()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    "initial,status,value",
    [
        ("unset NODE_OPTIONS", "absent", ""),
        ("export NODE_OPTIONS=", "value", ""),
        ("export NODE_OPTIONS='--require ./stop.cjs'", "value", "--require ./stop.cjs"),
        ("NODE_OPTIONS=unexported", "absent", ""),
        ("unset NODE_OPTIONS; export NODE_OPTIONS", "absent", ""),
        ("export NODE_OPTIONS='你好\n--no-warnings'", "value", "你好\n--no-warnings"),
        ("export NODE_OPTIONS=" + shlex.quote("é" * 2049), "unknown", ""),
        ("declare -ax NODE_OPTIONS=(one two)", "unknown", ""),
    ],
)
def test_round_trip_shell_assignment_states(backend, initial, status, value):
    env = backend(initial)
    fact = env.execution_context().node_options
    assert fact.status == status
    if status != "unknown":
        assert fact.value == value
    assert "--require" not in repr(env.execution_context())


@pytest.mark.platforms("posix")
def test_all_facts_and_npm_names_are_private(backend):
    sentinel = "SYNTHETIC_PRIVATE_VALUE"
    env = backend(
        "export HOME=/synthetic PATH=/usr/bin:/bin PREFIX=/prefix DESTDIR=; "
        "export NPM_CONFIG_Node_Options=" + sentinel
    )
    context = env.execution_context()
    assert context.home.value == "/synthetic"
    assert context.path.value == "/usr/bin:/bin"
    assert context.prefix.value == "/prefix"
    assert context.destdir.status == "value" and context.destdir.value == ""
    assert context.npm_env_names == frozenset({"npm_config_node_options"})
    trailer = Path(env._snapshot_path).read_bytes().splitlines()[-1]
    assert sentinel.encode() not in trailer
    assert sentinel not in repr(context) and "/synthetic" not in repr(context)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    "initial,safe",
    [("", True), ("export Path=/synthetic", False), ("export pAtH=", False)],
)
def test_npm_path_alias_names_are_attested_without_values(backend, initial, safe):
    env = backend(initial)
    assert env.execution_context().npm_path_safe is safe


@pytest.mark.platforms("posix")
def test_spawn_path_alias_invalidates_snapshot_attestation(backend, monkeypatch):
    env = backend()
    assert env.execution_context().npm_path_safe
    monkeypatch.setenv("Path", "/synthetic-alias")
    assert not env.execution_context().npm_path_safe


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    "name", ["NODE_OPTIONS", "HERMES_SESSION_CHAT_NAME", "HERMES_RPC_TOKEN"]
)
def test_readonly_exclusions_do_not_persist_values(tmp_path, bash, name):
    snapshot = tmp_path / "snapshot.sh"
    sentinel = "SYNTHETIC_PRIVATE_SENTINEL\nexport INJECTED=bad"
    dump = _export_dump_excluding_session_vars(
        shlex.quote(str(snapshot)), ("NODE_OPTIONS",)
    )
    script = f"readonly {name}={shlex.quote(sentinel)}\nexport {name}\n{dump}\n"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", script],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in snapshot.read_text()
    assert "INJECTED" not in snapshot.read_text()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("persistent", [True, False])
def test_persistence_republishes_actual_facts(backend, tmp_path, persistent):
    env = backend(persistent=persistent)
    (tmp_path / "safe.cjs").write_text("const value = 1;\n")
    (tmp_path / "stop.cjs").write_text(
        "require('child_process').execSync('hermes gateway restart');\n"
    )
    assert (
        env.execute("export NODE_OPTIONS='--require ./stop.cjs'", timeout=5)[
            "returncode"
        ]
        == 0
    )
    result = scan_gateway_lifecycle(
        "node safe.cjs", cwd=str(tmp_path), execution_context=env.execution_context()
    )
    assert result == (True, None) if persistent else result == (False, None)


@pytest.mark.platforms("posix")
def test_foreground_scan_never_executes(backend, tmp_path, monkeypatch):
    env = backend()
    (tmp_path / "safe.cjs").write_text("console.log('safe');\n")
    (tmp_path / "stop.cjs").write_text(
        "require('child_process').execSync('hermes gateway restart');\n"
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight attempted execution")

    monkeypatch.setattr(env, "execute", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    context = env.execution_context()
    assert scan_gateway_lifecycle(
        "node safe.cjs", cwd=str(tmp_path), execution_context=context
    ) == (False, None)
    assert scan_gateway_lifecycle(
        "NODE_OPTIONS='--require ./stop.cjs' node safe.cjs",
        cwd=str(tmp_path),
        execution_context=context,
    ) == (True, None)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "old",
        "malformed",
        "public",
        "symlink",
        "oversize",
        "not-ready",
        "duplicate",
    ],
)
def test_bad_snapshots_are_unknown(backend, tmp_path, kind):
    env = backend()
    path = Path(env._snapshot_path)

    def symlink():
        target = tmp_path / "other-snapshot"
        path.rename(target)
        path.symlink_to(target)

    def oversize():
        with path.open("ab") as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)

    actions = {
        "missing": path.unlink,
        "old": lambda: path.write_text("export NODE_OPTIONS=\n"),
        "malformed": lambda: path.write_text(
            "# hermes-exec-context v1 NODE_OPTIONS=a:zz\n"
        ),
        "public": lambda: path.chmod(0o644),
        "symlink": symlink,
        "oversize": oversize,
        "not-ready": lambda: setattr(env, "_snapshot_ready", False),
        "duplicate": lambda: path.write_bytes(
            path.read_bytes() + path.read_bytes().splitlines(keepends=True)[-1]
        ),
    }
    actions[kind]()
    context = env.execution_context()
    assert context.node_options.status == "unknown"
    assert context.node_options.reason.startswith("snapshot-")
    assert scan_gateway_lifecycle(
        "node -e 1", cwd=str(tmp_path), execution_context=context
    )[0]


@pytest.mark.platforms("posix")
def test_two_profiles_and_missing_key_use_actual_scoped_spawn_map(backend, monkeypatch):
    from agent import secret_scope as scope
    from tools import env_passthrough as passthrough

    monkeypatch.setattr(passthrough, "_config_passthrough", {})
    monkeypatch.setattr(
        passthrough, "_allowed_env_vars", {"NODE_OPTIONS", "NPM_CONFIG_Node_Options"}
    )
    scope.set_multiplex_active(True)
    try:
        token = scope.set_secret_scope(
            {"NODE_OPTIONS": "--no-warnings", "NPM_CONFIG_Node_Options": "private-a"}
        )
        try:
            env = backend("readonly NODE_OPTIONS; export NODE_OPTIONS")
            assert env.execution_context().node_options.value == "--no-warnings"
            snapshot = Path(env._snapshot_path).read_text()
            assert "--no-warnings" not in snapshot and "private-a" not in snapshot
        finally:
            scope.reset_secret_scope(token)
        token = scope.set_secret_scope({"NODE_OPTIONS": "--trace-warnings"})
        try:
            assert env.execution_context().node_options.value == "--trace-warnings"
            assert env.execution_context().npm_env_names == frozenset()
        finally:
            scope.reset_secret_scope(token)
        token = scope.set_secret_scope({})
        try:
            assert env.execution_context().node_options.status == "absent"
            assert env.execution_context().npm_env_names == frozenset()
        finally:
            scope.reset_secret_scope(token)
    finally:
        scope.set_multiplex_active(False)


@pytest.mark.platforms("posix")
def test_expanding_exclusions_ignore_stale_metadata(backend, monkeypatch):
    from agent import secret_scope as scope
    from tools import env_passthrough as passthrough

    env = backend("export NODE_OPTIONS=--no-warnings NPM_CONFIG_Node_Options=stale")
    monkeypatch.setattr(passthrough, "_config_passthrough", {})
    monkeypatch.setattr(
        passthrough, "_allowed_env_vars", {"NODE_OPTIONS", "NPM_CONFIG_Node_Options"}
    )
    scope.set_multiplex_active(True)
    token = scope.set_secret_scope({})
    try:
        context = env.execution_context()
        assert context.node_options.status == "absent"
        assert context.npm_env_names == frozenset()
    finally:
        scope.reset_secret_scope(token)
        scope.set_multiplex_active(False)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    "command,background,timeout",
    [
        ("node safe.cjs", True, 10),
        ("node safe.cjs &", False, 10),
        ("node safe.cjs", False, 601),
    ],
)
def test_background_routes_use_prepared_facts_without_guard_execution(
    backend, tmp_path, monkeypatch, command, background, timeout
):
    import json
    from tools import terminal_tool as terminal

    env = backend()
    (tmp_path / "safe.cjs").write_text("console.log('safe');\n")
    config = {
        "env_type": "local",
        "timeout": 180,
        "cwd": str(tmp_path),
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "modal_image": "",
        "daytona_image": "",
    }
    monkeypatch.setattr(terminal, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal, "_acquire_env", lambda *args: env)
    monkeypatch.setattr(terminal, "get_session_cwd", lambda *args: None)
    monkeypatch.setattr(
        "tools.process_registry._is_supervised_gateway_process", lambda: True
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("background preflight read or executed foreground context")

    monkeypatch.setattr(env, "execution_context", forbidden)
    monkeypatch.setattr(env, "execute", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    def accepted(**kwargs):
        launch = kwargs["prepared_launch"]
        assert launch.background and launch.context.node_options.status == "absent"
        return json.dumps({"output": "prepared guard passed", "error": None})

    monkeypatch.setattr(terminal, "spawn_background_process", accepted)
    result = json.loads(
        terminal.terminal_tool(command, background=background, timeout=timeout)
    )
    assert result["output"] == "prepared guard passed" and result["error"] is None
    if timeout > 600:
        assert result.get("promoted_from_foreground")



@pytest.mark.platforms("posix")
def test_effective_local_guard_uses_factory_context_without_execution(
    backend, tmp_path, monkeypatch
):
    from tools.terminal_tool_guards import gateway_lifecycle_block

    env = backend()
    (tmp_path / "safe.cjs").write_text("console.log('safe');\n")
    monkeypatch.setattr(
        "tools.process_registry._is_supervised_gateway_process", lambda: True
    )
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd", lambda *args: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("effective-local preflight executed a command")

    monkeypatch.setattr(env, "execute", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert (
        gateway_lifecycle_block(
            command="node safe.cjs",
            env=env,
            env_type="local",
            cwd=str(tmp_path),
            workdir=None,
            session_key="synthetic",
        )
        is None
    )


@pytest.mark.platforms("posix")
def test_private_option_errors_and_stats_never_expose_values(backend, tmp_path, caplog):
    env = backend("export NODE_OPTIONS='--require ./SYNTHETIC_PRIVATE_SELECTOR'")
    stats = {}
    unsafe, reason = scan_gateway_lifecycle(
        "node -e 1",
        cwd=str(tmp_path),
        execution_context=env.execution_context(),
        scan_stats=stats,
    )
    assert (
        unsafe
        and reason == "private Node execution options could not be completely inspected"
    )
    assert "SYNTHETIC_PRIVATE_SELECTOR" not in repr((reason, stats)) + caplog.text


@pytest.mark.platforms("posix")
def test_npm_name_bound_never_becomes_empty(backend):
    initial = "; ".join(f"export npm_config_key_{index}=value" for index in range(65))
    assert backend(initial).execution_context().npm_env_names is None


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    "initial,expected",
    [
        ("unset NODE_OPTIONS", "--trace-warnings"),
        ("NODE_OPTIONS=unexported", "--trace-warnings"),
        ("unset NODE_OPTIONS; export NODE_OPTIONS", "--trace-warnings"),
        ("export NODE_OPTIONS=", ""),
    ],
)
def test_inherited_spawn_values_differ_from_assigned_empty(
    backend, monkeypatch, initial, expected
):
    env = backend(initial)
    monkeypatch.setenv("NODE_OPTIONS", "--trace-warnings")
    fact = env.execution_context().node_options
    assert fact.status == "value" and fact.value == expected


@pytest.mark.platforms("posix")
def test_readonly_stale_assignment_cannot_attest_new_passthrough(backend, monkeypatch):
    from agent import secret_scope as scope
    from tools import env_passthrough as passthrough

    env = backend("readonly NODE_OPTIONS=--no-warnings; export NODE_OPTIONS")
    monkeypatch.setattr(passthrough, "_config_passthrough", {})
    monkeypatch.setattr(passthrough, "_allowed_env_vars", {"NODE_OPTIONS"})
    scope.set_multiplex_active(True)
    token = scope.set_secret_scope({})
    try:
        fact = env.execution_context().node_options
        assert (
            fact.status == "unknown"
            and fact.reason == "snapshot-excluded-state-unsupported"
        )
    finally:
        scope.reset_secret_scope(token)
        scope.set_multiplex_active(False)


@pytest.mark.platforms("posix")
def test_scope_failure_is_unknown_without_private_error_text(
    backend, monkeypatch, caplog
):
    env = backend()

    def failed(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_SCOPE_FAILURE")

    monkeypatch.setattr(env, "_snapshot_excluded_passthrough_names", failed)
    fact = env.execution_context().node_options
    assert fact.status == "unknown" and fact.reason == "execution-scope-unavailable"
    assert "SYNTHETIC_PRIVATE_SCOPE_FAILURE" not in repr(fact) + caplog.text


@pytest.mark.platforms("posix")
def test_path_fact_does_not_choose_the_encoder(backend, tmp_path):
    tools = tmp_path / "synthetic-tools"
    tools.mkdir()
    marker = tmp_path / "encoder-shim-ran"
    shim = tools / "od"
    shim.write_text("#!/bin/sh\nprintf wrong > " + shlex.quote(str(marker)) + "\n")
    shim.chmod(0o755)
    task_path = str(tools) + ":/usr/bin:/bin"
    context = backend("export PATH=" + shlex.quote(task_path)).execution_context()
    assert context.path.status == "value" and context.path.value == task_path
    assert not marker.exists()


@pytest.mark.platforms("posix")
def test_spawn_map_npm_names_preserve_hyphen_aliases(backend, monkeypatch):
    env = backend()
    monkeypatch.setenv("npm_config_node-options", "SYNTHETIC_PRIVATE_VALUE")
    context = env.execution_context()
    assert context.npm_env_names == frozenset({"npm_config_node-options"})
    assert "SYNTHETIC_PRIVATE_VALUE" not in repr(context)


@pytest.mark.platforms("posix")
def test_disabled_compgen_cannot_publish_snapshot(backend):
    env = backend()
    path = Path(env._snapshot_path)
    before = path.read_bytes()
    result = env.execute("enable -n compgen", timeout=5)
    assert result["returncode"] == 0
    assert path.read_bytes() == before


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("name", ["BASH_ENV"])
def test_snapshot_hook_names_are_not_attested_absent(backend, name):
    env = backend("export " + name + "=/SYNTHETIC_NEVER_OPENED")
    context = env.execution_context()
    unsafe, reason = scan_gateway_lifecycle(
        "bash -c 'node -e 1'", execution_context=context
    )
    assert unsafe and reason is not None
    assert "SYNTHETIC_NEVER_OPENED" not in repr(context)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("name", ["BASH_ENV"])
def test_actual_outer_spawn_hook_makes_context_unknown(backend, monkeypatch, name):
    env = backend()
    monkeypatch.setenv(name, "/SYNTHETIC_NEVER_OPENED")
    context = env.execution_context()
    assert context.node_options.status == "unknown"
    assert "SYNTHETIC_NEVER_OPENED" not in repr(context)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("persistent", [True, False])
def test_late_readonly_exclusion_restores_current_scope_and_other_state(
    tmp_path, bash, persistent
):
    from tools.environments.base_session_env import _wrap_command_script

    snapshot = tmp_path / "snapshot.sh"
    q_snapshot = shlex.quote(str(snapshot))
    prior = "SYNTHETIC_OLD_SCOPE\nexport LEAKED=bad"
    unrelated = "SYNTHETIC_OTHER_STATE\nsecond line"
    saved_code = 'saved_fn () { test "$1" = retained; }\nalias saved_alias=true\n'
    first = (
        "readonly NODE_OPTIONS=" + shlex.quote(prior) + "\nexport NODE_OPTIONS\n"
        "readonly SAVED="
        + shlex.quote(unrelated)
        + "\nexport SAVED\n"
        + _export_dump_excluding_session_vars(q_snapshot, ())
        + "\nprintf %s "
        + shlex.quote(saved_code)
        + " >> "
        + q_snapshot
    )
    minimal = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    setup = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", first],
        env=minimal,
        cwd=tmp_path,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert setup.returncode == 0
    command = (
        'test "${NODE_OPTIONS-unset}" = current && test -z "${LEAKED-}" && '
        'test "$SAVED" = '
        + shlex.quote(unrelated)
        + " && saved_fn retained && alias saved_alias >/dev/null && "
        "! (SAVED=changed) 2>/dev/null"
    )
    wrapper = _wrap_command_script(
        command,
        quoted_cwd=shlex.quote(str(tmp_path)),
        quoted_snap=q_snapshot,
        snap_tmp_template=shlex.quote(str(snapshot) + ".tmp.XXXXXXXXXX"),
        passthrough_names=("NODE_OPTIONS",),
        snapshot_ready=True,
        cwd_marker="SYNTHETIC_CWD",
        persist_env=persistent,
    )
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", wrapper],
        env={**minimal, "NODE_OPTIONS": "current"},
        cwd=tmp_path,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert prior.encode() not in result.stdout + result.stderr
    if persistent:
        assert b"SYNTHETIC_OLD_SCOPE" not in snapshot.read_bytes()
        assert b"SYNTHETIC_OTHER_STATE" in snapshot.read_bytes()


@pytest.mark.platforms("posix")
def test_absent_hook_producer_supports_nested_bash_and_sh(backend, tmp_path):
    context = backend().execution_context()
    assert context.shell_startup_safe
    for command in ("bash -c 'node -e 1'", "sh -c 'node -e 1'"):
        assert scan_gateway_lifecycle(
            command, cwd=str(tmp_path), execution_context=context
        ) == (False, None)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("name", ["ENV", "ZDOTDIR"])
@pytest.mark.parametrize("origin", ["snapshot", "spawn"])
def test_unrelated_shell_hook_does_not_invalidate_foreground_bash(
    backend, monkeypatch, tmp_path, name, origin
):
    from tests.cron.lifecycle_context_fixtures import npm_context

    npm = npm_context(tmp_path)
    initial = "export PATH=" + shlex.quote(npm.path.value + ":/usr/bin:/bin")
    initial += " HOME=" + shlex.quote(npm.home.value)
    if origin == "snapshot":
        initial += "; export " + name + "=production"
    env = backend(initial)
    if origin == "spawn":
        monkeypatch.setenv(name, "production")
    context = env.execution_context()
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node -e 1"}}')
    for command in ("node -e 1", "npm test"):
        assert scan_gateway_lifecycle(
            command, cwd=str(tmp_path), execution_context=context
        ) == (False, None)
    unsafe, reason = scan_gateway_lifecycle(
        "zsh -c 'node -e 1'", cwd=str(tmp_path), execution_context=context
    )
    assert unsafe and reason is not None


@pytest.mark.platforms("posix")
def test_missing_snapshot_after_admission_stops_then_recovers(backend, tmp_path):
    from tools.environments.base_session_env import SNAPSHOT_RESTORE_ERROR

    env = backend("export NODE_OPTIONS=--no-warnings")
    assert env.execution_context().node_options.status == "value"
    Path(env._snapshot_path).unlink()
    counter = tmp_path / "runs"
    command = "printf run >> " + shlex.quote(str(counter))
    result = env.execute(command, timeout=5)
    assert result["returncode"] == 126 and not counter.exists()
    assert SNAPSHOT_RESTORE_ERROR in result["output"]
    assert "__HERMES_RESTORE" not in result["output"]
    assert not env._snapshot_ready
    assert env.execution_context().node_options.status == "unknown"
    result = env.execute(command, timeout=5)
    assert result["returncode"] == 0 and counter.read_text() == "run"
    assert result["snapshot_recovery"] == "ready"
    assert result["environment_recreated"]
    assert env._snapshot_ready
    assert env.execution_context().node_options.status != "unknown"


@pytest.mark.platforms("posix")
def test_missing_snapshot_before_admission_invalidates_owner(backend, tmp_path):
    env = backend()
    Path(env._snapshot_path).unlink()
    context = env.execution_context()
    assert context.node_options.reason == "snapshot-missing"
    assert not env._snapshot_ready
    unsafe, reason = scan_gateway_lifecycle(
        "node -e 1", cwd=str(tmp_path), execution_context=context
    )
    assert unsafe and reason == "execution-affecting Node options unavailable from actual execution context (snapshot-missing)"
    result = env.execute("printf recovered", timeout=5)
    assert result["returncode"] == 0 and result["output"] == "recovered"
    assert env._snapshot_ready


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("persistent", [True, False])
def test_failed_restore_copy_refuses_until_repaired_without_state_loss(
    backend, tmp_path, monkeypatch, persistent
):
    from tools.environments.base_session_env import SNAPSHOT_RESTORE_ERROR

    env = backend("export SAVED=retained; saved_fn () { :; }", persistent=persistent)
    path = Path(env._snapshot_path)
    before = path.read_bytes()
    original = env._snapshot_script_kwargs

    def unavailable(cwd):
        kwargs = original(cwd)
        kwargs["snap_tmp_template"] = shlex.quote(str(tmp_path / "missing" / "copy.XXXXXX"))
        return kwargs

    monkeypatch.setattr(env, "_snapshot_script_kwargs", unavailable)
    counter = tmp_path / "runs"
    command = "printf run >> " + shlex.quote(str(counter))
    result = env.execute(command, timeout=5)
    assert result["returncode"] == 126 and not counter.exists()
    assert SNAPSHOT_RESTORE_ERROR in result["output"]
    assert path.read_bytes() == before and not env._snapshot_ready
    assert env.execution_context().node_options.reason == "snapshot-not-ready"
    unsafe, reason = scan_gateway_lifecycle(
        "node -e 1", cwd=str(tmp_path), execution_context=env.execution_context()
    )
    assert unsafe and reason == "execution-affecting Node options unavailable from actual execution context (snapshot-not-ready)"
    result = env.execute(command, timeout=5)
    assert result["returncode"] == 126 and not counter.exists()
    assert not env._snapshot_ready and path.read_bytes() == before
    monkeypatch.setattr(env, "_snapshot_script_kwargs", original)
    assert env.execute(":", timeout=5)["returncode"] == 0
    assert env._snapshot_ready and path.read_bytes() == before
    result = env.execute('test "$SAVED" = retained && saved_fn', timeout=5)
    assert result["returncode"] == 0
    assert not list(path.parent.glob(path.name + ".tmp.*"))


@pytest.mark.platforms("posix")
def test_bash_nameref_really_mutates_indirect_execution_fact(bash, tmp_path):
    from tests.cron.lifecycle_context_fixtures import known_context

    command = "declare -n R=NODE_OPTIONS; R=changed"
    script = (
        'if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3) )); '
        "then exit 77; fi\n" + command + '\nbuiltin test "$NODE_OPTIONS" = changed'
    )
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", script],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True, timeout=5, check=False,
    )
    if result.returncode == 77:
        pytest.skip("Namerefs require Bash 4.3 or later; scanner coverage is unconditional")
    assert result.returncode == 0
    assert scan_gateway_lifecycle(
        command + "; node -e 1", cwd=str(tmp_path), execution_context=known_context()
    ) == (True, "execution context changed earlier in this command")
