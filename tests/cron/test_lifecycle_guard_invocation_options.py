"""FBL-370: invocation-local NODE_OPTIONS facts, without executing inspected code."""
import json
import shlex
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.fixture
def project(tmp_path):
    (tmp_path / "safe.cjs").write_text("console.log('safe');\n")
    (tmp_path / "stop.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"check": "echo done", "node": "node safe.cjs"}}))
    return tmp_path


@pytest.mark.parametrize("command", ["npm run check", "npm --prefix package run check"])
def test_npm_startup_preloads_use_invocation_directory(project, command):
    package = project / "package"
    package.mkdir()
    (package / "package.json").write_text('{"scripts":{"check":"echo done"}}')
    assert scan_gateway_lifecycle(command, cwd=str(project), execution_options="--require ./stop.cjs") == (True, None)


def test_npm_unknown_context_stays_unknown(project):
    unsafe, reason = scan_gateway_lifecycle("npm run check", cwd=str(project), execution_options=None)
    assert unsafe and "actual execution context" in reason


@pytest.mark.parametrize("prefix", [
    "env -u NODE_OPTIONS", "env --unset NODE_OPTIONS", "env --unset=NODE_OPTIONS", "env -uNODE_OPTIONS",
    "env -i", "env --ignore-environment", "env -", "sudo -u nobody env -u NODE_OPTIONS --",
    "NODE_OPTIONS='--require ./stop.cjs' env -u NODE_OPTIONS", "env -- NODE_OPTIONS=",
    "env -S '-u NODE_OPTIONS node'",
])
def test_explicit_env_operations_clear_incoming_options(project, prefix):
    command = prefix + (" safe.cjs" if prefix.endswith("node'") else " node safe.cjs")
    assert scan_gateway_lifecycle(command, cwd=str(project), execution_options="--require ./stop.cjs") == (False, None)


@pytest.mark.parametrize("prefix", [
    "env -u NODE_OPTIONS NODE_OPTIONS='--require ./stop.cjs'",
    "env -i -- NODE_OPTIONS='--require ./stop.cjs'",
    "NODE_OPTIONS= env NODE_OPTIONS='--require ./stop.cjs'",
])
def test_env_assignments_override_unset_and_incoming_prefix(project, prefix):
    assert scan_gateway_lifecycle(prefix + " node safe.cjs", cwd=str(project), execution_options="") == (True, None)


def test_wrapper_option_values_are_not_assignments(project):
    command = "sudo -p 'NODE_OPTIONS=--require ./stop.cjs' node safe.cjs"
    assert scan_gateway_lifecycle(command, cwd=str(project), execution_options="") == (False, None)


@pytest.mark.parametrize("command", [
    "env -u NODE_OPTIONS sh -c 'node safe.cjs'", "env -i npm run node",
    "env -u NODE_OPTIONS ./entry", "env -- NODE_OPTIONS= sh -c 'node safe.cjs'",
])
def test_env_options_propagate_to_children_and_shebangs(project, command):
    (project / "entry").write_text("#!/usr/bin/env node\nconsole.log('safe');\n")
    assert scan_gateway_lifecycle(command, cwd=str(project), execution_options="--require ./stop.cjs") == (False, None)


@pytest.mark.parametrize("shebang", ["env -u NODE_OPTIONS node", "env -S -u NODE_OPTIONS node", "env -i node"])
def test_shebang_env_clears_options_for_child_commands(project, shebang):
    (project / "entry").write_text("#!/usr/bin/" + shebang + "\nrequire('child_process').execSync('node safe.cjs');\n")
    assert scan_gateway_lifecycle("./entry", cwd=str(project), execution_options="--require ./stop.cjs") == (False, None)


@pytest.mark.parametrize("command,reason", [
    ("env -u $NAME node safe.cjs", "unset selector"),
    ("env NODE_OPTIONS=$OPTIONS node safe.cjs", "NODE_OPTIONS"),
    ("env --mystery node safe.cjs", "unsupported env"),
    ("env -C sub node safe.cjs", "unsupported env"),
    ("env -u", "value missing"),
    (r"env -S 'node\_stop.cjs'", "env split-string"),
])
def test_unresolved_env_operations_refuse(project, command, reason):
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(project), execution_options="")
    assert unsafe and reason in refusal


def test_unsupported_shebang_env_operation_refuses(project):
    (project / "entry").write_text("#!/usr/bin/env -C sub node\nconsole.log('safe');\n")
    unsafe, reason = scan_gateway_lifecycle("./entry", cwd=str(project), execution_options="")
    assert unsafe and "unsupported env" in reason


@pytest.mark.parametrize("shebang", [r"env -S node\_stop.cjs", "env $RUNTIME", "env --mystery sh"])
def test_unresolved_shebang_operations_do_not_disappear(project, shebang):
    (project / "entry").write_text("#!/usr/bin/" + shebang + "\necho done\n")
    unsafe, reason = scan_gateway_lifecycle("./entry", cwd=str(project), execution_options="")
    assert unsafe and reason is not None


def test_shell_shebang_env_operation_propagates(project):
    (project / "entry").write_text("#!/usr/bin/env -u NODE_OPTIONS sh\nnode safe.cjs\n")
    assert scan_gateway_lifecycle("./entry", cwd=str(project), execution_options="--require ./stop.cjs") == (False, None)


@pytest.mark.parametrize("setter", [
    "export NODE_OPTIONS='--require ./stop.cjs'", "unset NODE_OPTIONS", "NODE_OPTIONS=",
    "declare -x NODE_OPTIONS='--require ./stop.cjs'", "readonly NODE_OPTIONS=", "builtin export NODE_OPTIONS=",
    "NODE_OPTIONS+=--no-warnings", "unset $NAME", "export $NAME=--no-warnings",
    "eval 'export NODE_OPTIONS=--no-warnings'", "eval \"eval 'unset NODE_OPTIONS'\"",
    "eval \"sh -c 'export NODE_OPTIONS=--no-warnings'\"",
])
@pytest.mark.parametrize("command", ["node safe.cjs", "npm run check"])
def test_inline_mutations_refuse_affected_invocations(project, setter, command):
    unsafe, reason = scan_gateway_lifecycle(setter + "; " + command, cwd=str(project), execution_options="")
    assert unsafe and "inline NODE_OPTIONS shell mutation" in reason


@pytest.mark.parametrize("payload", [
    "export NODE_OPTIONS=", "unset NODE_OPTIONS", "eval 'export NODE_OPTIONS='",
])
def test_inline_mutations_inside_shell_payload_refuse(project, payload):
    command = "sh -c " + shlex.quote(payload + "; node safe.cjs")
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(project), execution_options="")
    assert unsafe and reason is not None


@pytest.mark.parametrize("command", [
    "export NODE_OPTIONS=; echo done", "echo 'export NODE_OPTIONS=; unset NODE_OPTIONS'; node safe.cjs",
    "eval 'echo NODE_OPTIONS'; node safe.cjs", "export OTHER=1; node safe.cjs",
    "export NODE_OPTIONS=; env -i node safe.cjs",
])
def test_mutation_refusals_are_limited_to_affected_contexts(project, command):
    assert scan_gateway_lifecycle(command, cwd=str(project), execution_options="") == (False, None)


def test_javascript_exports_and_data_are_not_shell_mutations(project):
    (project / "safe.cjs").write_text("export const NODE_OPTIONS = '';\nconst text = 'export NODE_OPTIONS=bad';\n")
    assert scan_gateway_lifecycle("node safe.cjs", cwd=str(project), execution_options="") == (False, None)


@pytest.mark.parametrize("payload", [
    "export NODE_OPTIONS=; node safe.cjs", "unset NODE_OPTIONS; npm run check",
    "NODE_OPTIONS=; node safe.cjs", "eval 'export NODE_OPTIONS='; node safe.cjs",
])
def test_executed_javascript_shell_literals_track_mutations(project, payload):
    (project / "safe.cjs").write_text("require('child_process').execSync(" + json.dumps(payload) + ");\n")
    unsafe, reason = scan_gateway_lifecycle("node safe.cjs", cwd=str(project), execution_options="")
    assert unsafe and "inline NODE_OPTIONS shell mutation" in reason


def test_inert_javascript_mutation_string_does_not_taint_child(project):
    (project / "safe.cjs").write_text(
        "const text = 'export NODE_OPTIONS=bad';\nrequire('child_process').execSync('node -e 1');\n"
    )
    assert scan_gateway_lifecycle("node safe.cjs", cwd=str(project), execution_options="") == (False, None)


def test_completed_source_is_rechecked_under_new_options(project):
    (project / "child.cjs").write_text("require('child_process').execSync('node safe.cjs');\n")
    command = "node child.cjs; NODE_OPTIONS='--require ./stop.cjs' node child.cjs"
    assert scan_gateway_lifecycle(command, cwd=str(project), execution_options="") == (True, None)


def test_pure_scans_do_not_execute_preloads_or_commands(project, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure source scan attempted process execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert scan_gateway_lifecycle("npm run check", cwd=str(project), execution_options="--require ./stop.cjs") == (True, None)
    assert scan_gateway_lifecycle("env -i npm run node", cwd=str(project), execution_options=None) == (False, None)
