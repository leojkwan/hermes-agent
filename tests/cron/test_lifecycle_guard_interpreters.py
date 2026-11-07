"""FBL-370: interpreter entry, static options, preloads, and inline source."""
import pytest

from tests.cron.lifecycle_context_fixtures import known_context, npm_context

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.parametrize("command", ["node entry.cjs", "node -- entry.cjs", "node --title benign entry.cjs",
    "node --title=benign entry.cjs", "node -r ./entry.cjs -e 1", "node --require=./entry.cjs -e 1",
    "node --import ./entry.cjs -p 1", "node --import=./entry.cjs -e 1",
    "node -e 1 --require ./entry.cjs", "python3 entry.py"])
def test_entry_and_preload_reach_helpers(tmp_path, command):
    (tmp_path / "entry.cjs").write_text("dynamicImport('./helper.cjs');\n")
    (tmp_path / "entry.py").write_text("import os\nos.system('node helper.cjs')\n")
    (tmp_path / "helper.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), execution_context=known_context()) == (True, None)


@pytest.mark.parametrize("flag", ["-e", "--eval", "-p", "--print"])
@pytest.mark.parametrize("equals", [False, True])
def test_inline_evaluation_inspects_literal_paths(tmp_path, flag, equals):
    (tmp_path / "evil.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    value = "\"require('./evil.cjs')\""
    command = "node " + flag + ("=" if equals else " ") + value
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), execution_context=known_context()) == (True, None)


def test_explicit_options_and_inline_assignment(tmp_path):
    (tmp_path / "evil.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    (tmp_path / "okay.cjs").write_text("const value = 1;\n")
    for command, options in [("node okay.cjs", "--require ./evil.cjs"),
                             ("NODE_OPTIONS='--import ./evil.cjs' node okay.cjs", "")]:
        assert scan_gateway_lifecycle(command, cwd=str(tmp_path), execution_context=known_context(options)) == (True, None)


@pytest.mark.parametrize("command,reason", [
    ("node --require $MODULE -e 1", "dynamic"), ("node $ENTRY", "dynamic"),
    ("node --loader ./loader.cjs entry.cjs", "routing"), ("node --require", "value"),
    ("node missing.cjs", "missing"), ("node --import=https://example.invalid/x -e 1", "unresolved"),
])
def test_unknowable_node_routes_refuse(tmp_path, command, reason):
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and reason in refusal


def test_missing_execution_context_refuses(tmp_path):
    unsafe, refusal = scan_gateway_lifecycle("node -e 1", cwd=str(tmp_path), execution_context=None)
    assert unsafe and "execution context" in refusal


def test_node_builtins_and_benign_options(tmp_path):
    (tmp_path / "entry.cjs").write_text("const node = 1;\nfunction f(node) { return node !== 0; }\n")
    assert scan_gateway_lifecycle("node --require node:fs --conditions=development entry.cjs", cwd=str(tmp_path), execution_context=known_context()) == (False, None)


@pytest.mark.parametrize("payload", ["node helper.cjs", "npm run check"])
def test_source_literal_shell_invocation_is_additive(tmp_path, payload):
    (tmp_path / "entry.cjs").write_text("execSync(" + repr(payload) + ");\n")
    (tmp_path / "helper.cjs").write_text("require('child_process').execSync('hermes gateway restart');\n")
    (tmp_path / "package.json").write_text('{"scripts":{"check":"hermes gateway restart"}}')
    assert scan_gateway_lifecycle("node entry.cjs", cwd=str(tmp_path), execution_context=npm_context(tmp_path)) == (True, None)


def test_shebang_static_preload_and_explicit_options(tmp_path):
    (tmp_path / "helper.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    (tmp_path / "entry").write_text("#!/usr/bin/env -S node --require ./helper.cjs\nconst value = 1;\n")
    assert scan_gateway_lifecycle("./entry", cwd=str(tmp_path), execution_context=known_context()) == (True, None)
    (tmp_path / "entry").write_text("#!/usr/bin/env node\nconst value = 1;\n")
    assert scan_gateway_lifecycle("NODE_OPTIONS='--import ./helper.cjs' ./entry", cwd=str(tmp_path), execution_context=known_context()) == (True, None)


@pytest.mark.parametrize("command", ["node", "node -", "node -- -"])
def test_unavailable_stdin_or_repl_source_refuses(tmp_path, command):
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and ("stdin" in reason or "selector" in reason)


def test_builtin_prefix_does_not_exempt_same_name_package_subpath(tmp_path):
    module = tmp_path / "node_modules/fs"
    module.mkdir(parents=True)
    (module / "evil.js").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    assert scan_gateway_lifecycle("node --require fs/evil -e 1", cwd=str(tmp_path), execution_context=known_context()) == (True, None)


@pytest.mark.parametrize("payload", ["bash helper.sh", "sudo bash helper.sh", "./helper.sh --check"])
def test_valid_node_shell_literals_reach_lifecycle_helper(tmp_path, payload):
    (tmp_path / "helper.sh").write_text("hermes gateway restart\n")
    (tmp_path / "entry.cjs").write_text("require('node:child_process').execSync(" + repr(payload) + ");\n")
    assert scan_gateway_lifecycle("node entry.cjs", cwd=str(tmp_path), execution_context=known_context()) == (True, None)


@pytest.mark.parametrize("command", ["node scripts/entry.cjs", "node --require ./scripts/module.cjs -e 1"])
def test_module_directory_and_child_process_cwd_are_distinct(tmp_path, command):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "helper.sh").write_text("hermes gateway restart\n")
    (scripts / "module.cjs").write_text("require('node:child_process').execSync('bash helper.sh');\n")
    (scripts / "entry.cjs").write_text("require('./module.cjs');\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), execution_context=known_context()) == (True, None)


def test_eval_is_javascript_source_not_a_shell_command(tmp_path):
    assert scan_gateway_lifecycle('node -e "function f(node) { return node; }"', cwd=str(tmp_path), execution_context=known_context()) == (False, None)


def test_documented_node_commands_are_source_data(tmp_path):
    (tmp_path / "entry.cjs").write_text("const label = 'node '; const help = 'node --help';\n")
    assert scan_gateway_lifecycle("node entry.cjs", cwd=str(tmp_path), execution_context=known_context()) == (False, None)
