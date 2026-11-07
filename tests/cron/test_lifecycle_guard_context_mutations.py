"""FBL-370 reviewed shell and npm admission regressions. No callers execute."""

from dataclasses import replace

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.lifecycle_context_fixtures import known_context, npm_context


@pytest.mark.parametrize(
    "command",
    [
        "npm t",
        "npm tst",
        "npm tes",
        "npm test",
        "npm rum test",
        "npm urn test",
        "npm run-script test",
    ],
)
def test_supported_npm_spellings_inspect_actual_test_hook(tmp_path, command):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"bash helper.sh"}}')
    (tmp_path / "helper.sh").write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=npm_context(tmp_path)
    ) == (True, None)


@pytest.mark.parametrize(
    "command",
    [
        "install",
        "ci",
        "it",
        "install-test",
        "cit",
        "exec",
        "x",
        "explore",
        "rebuild",
        "pack",
        "publish",
        "version",
        "r",
        "s",
        "unknown",
        "--help",
        "--versions",
    ],
)
def test_unmodeled_npm_commands_never_fall_through_clean(tmp_path, command):
    unsafe, reason = scan_gateway_lifecycle(
        "npm " + command, cwd=str(tmp_path), execution_context=npm_context(tmp_path)
    )
    assert unsafe and reason is not None


@pytest.mark.parametrize(
    "command",
    [
        "if true; then export NODE_OPTIONS=--require=./evil.cjs; fi; node safe.cjs",
        "{ export NODE_OPTIONS=--require=./evil.cjs; }; node safe.cjs",
        "! export NODE_OPTIONS=changed; node safe.cjs",
        "time export NODE_OPTIONS=changed; node safe.cjs",
        "while false; do export NODE_OPTIONS=changed; done; node safe.cjs",
        "printf -v PATH %s /synthetic; npm test",
        "read HOME <<< /synthetic; npm test",
        "mapfile PATH; npm test",
        "readarray HOME; npm test",
        "getopts x HOME; npm test",
        "let PATH=1; npm test",
        "for HOME in /synthetic; do true; done; npm test",
        "select PATH in /synthetic; do true; done; npm test",
        'echo "${NODE_OPTIONS:=changed}"; node safe.cjs',
        ": ${HOME=/synthetic}; npm test",
        "case x in x) true;; esac; node safe.cjs",
    ],
)
def test_static_execution_fact_mutations_refuse(tmp_path, command):
    (tmp_path / "safe.cjs").write_text("const value = 1;\n")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node -e 1"}}')
    unsafe, reason = scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=npm_context(tmp_path)
    )
    assert unsafe and reason == "execution context changed earlier in this command"


@pytest.mark.parametrize(
    "command",
    [
        "then node stop.cjs",
        "{ node stop.cjs; }",
        "! node stop.cjs",
        "if node stop.cjs; then true; fi",
    ],
)
def test_reserved_prefixes_discover_node_callees(tmp_path, command):
    (tmp_path / "stop.cjs").write_text("execSync('hermes gateway restart');\n")
    assert scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=known_context()
    ) == (True, None)


@pytest.mark.parametrize(
    "command",
    [
        "echo '${NODE_OPTIONS:=inert}'; node -e 1",
        "echo 'then export NODE_OPTIONS=changed'; node -e 1",
        "echo 'printf -v PATH'; node -e 1",
        "cat <<'TEXT'\n${NODE_OPTIONS:=inert}\nTEXT\nnode -e 1",
    ],
)
def test_inert_shell_text_does_not_mutate_context(tmp_path, command):
    assert scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=known_context()
    ) == (False, None)


@pytest.mark.parametrize(
    "command",
    [
        "BASH_ENV=./init.sh bash -c 'node -e 1'",
        "export BASH_ENV=./init.sh; bash -c 'node -e 1'",
        "ENV=./init.sh bash -c 'node -e 1'",
        "ZDOTDIR=/synthetic bash -c 'node -e 1'",
        "zsh -c 'node -e 1'",
    ],
)
def test_startup_hooks_cannot_inherit_absent_node_options(tmp_path, command):
    unsafe, reason = scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and reason is not None


def test_plain_sh_payload_remains_supported(tmp_path):
    assert scan_gateway_lifecycle(
        "sh -c 'node -e 1'", cwd=str(tmp_path), execution_context=known_context()
    ) == (False, None)


def test_bash_requires_explicit_absent_hook_attestation(tmp_path):
    context = replace(known_context(), shell_startup_safe=False)
    unsafe, reason = scan_gateway_lifecycle(
        "bash -c 'node -e 1'", cwd=str(tmp_path), execution_context=context
    )
    assert unsafe and reason is not None
    assert scan_gateway_lifecycle(
        "bash -c 'node -e 1'",
        cwd=str(tmp_path),
        execution_context=replace(context, shell_startup_safe=True),
    ) == (False, None)


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash"])
def test_shebang_requires_shell_initialization_facts(tmp_path, shell):
    (tmp_path / "entry").write_text("#!" + shell + "\nnode -e 1\n")
    unsafe, reason = scan_gateway_lifecycle(
        "./entry",
        cwd=str(tmp_path),
        execution_context=replace(known_context(), shell_startup_safe=False),
    )
    assert unsafe and reason is not None


@pytest.mark.parametrize(
    "command",
    [
        "((PATH++)); node -e 1",
        "read -ra PATH; node -e 1",
        "builtin printf -v HOME %s /synthetic; node -e 1",
    ],
)
def test_other_static_assignment_forms_do_not_reuse_facts(tmp_path, command):
    unsafe, reason = scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and reason == "execution context changed earlier in this command"


@pytest.mark.parametrize(
    "command",
    [
        "npm t",
        "npm tst",
        "npm tes",
        "npm rum test",
        "npm urn test",
        "npm run-script test",
    ],
)
def test_supported_npm_spellings_keep_neutral_nested_graphs(tmp_path, command):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"npm run inner","inner":"node -e 1"}}'
    )
    assert scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=npm_context(tmp_path)
    ) == (False, None)


def test_same_segment_overlay_cannot_erase_parameter_mutation(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node -e 1"}}')
    unsafe, reason = scan_gateway_lifecycle(
        "NODE_OPTIONS= X=${HOME:=/synthetic} npm test",
        cwd=str(tmp_path),
        execution_context=npm_context(tmp_path),
    )
    assert unsafe and reason == "execution context changed earlier in this command"


@pytest.mark.parametrize("command, reason", [
    ("declare -n R=PATH; R=/synthetic; npm test", "execution context changed earlier in this command"),
    ("declare -gn R=NODE_OPTIONS; R=--require=./evil.cjs; node safe.cjs", "execution context changed earlier in this command"),
    ("local -rn R=BASH_ENV; read R; bash -c 'node -e 1'", "execution-affecting Node options unavailable from actual execution context (shell-initialization-context-unavailable)"),
    ("typeset -nx R=HOME; printf -v R %s /synthetic; npm test", "execution context changed earlier in this command"),
    ("declare -n R=BASH_ENV; R=./init.sh; bash -c 'node -e 1'", "execution-affecting Node options unavailable from actual execution context (shell-initialization-context-unavailable)"),
    ("set -k; node safe.cjs NODE_OPTIONS=--require=./evil.cjs", "execution context changed earlier in this command"),
    ("set -ek; npm test HOME=/synthetic", "execution context changed earlier in this command"),
    ("set -o keyword; bash -c 'node -e 1' BASH_ENV=./init.sh", "execution-affecting Node options unavailable from actual execution context (shell-initialization-context-unavailable)"),
    ("set -ko keyword; node safe.cjs", "execution context changed earlier in this command"),
    ('echo "$((PATH=0))"; npm test', "execution context changed earlier in this command"),
    ('set -- "$((PATH=0))"; npm test', "execution context changed earlier in this command"),
    (": $[HOME=0]; npm test", "execution context changed earlier in this command"),
    (": ${a[PATH=0]}; npm test", "execution context changed earlier in this command"),
    ('echo "${a[NODE_OPTIONS=0]}"; node safe.cjs', "execution context changed earlier in this command"),
    ("true {PATH}>/dev/null; npm test", "execution context changed earlier in this command"),
    ("true {NODE_OPTIONS}>&-; node safe.cjs", "execution context changed earlier in this command"),
    ("wait -n -p PATH; npm test", "execution context changed earlier in this command"),
    ("wait -pHOME; npm test", "execution context changed earlier in this command"),
    ("wait -np PATH; npm test", "execution context changed earlier in this command"),
    ("trap 'export PATH=/synthetic' DEBUG; npm test", "execution context changed earlier in this command"),
    ("trap 'export NODE_OPTIONS=--require=./evil.cjs' EXIT; node safe.cjs", "execution context changed earlier in this command"),
    ("echo a#${HOME:=/synthetic}; npm test", "execution context changed earlier in this command"),
])
def test_reviewed_indirect_and_expansion_mutations_refuse(tmp_path, command, reason):
    (tmp_path / "safe.cjs").write_text("const value = 1;\n")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node -e 1"}}')
    assert scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=npm_context(tmp_path)
    ) == (True, reason)


@pytest.mark.parametrize("prefix", [
    "curl http://localhost:3000/#/; ",
    "echo word#fragment; ",
    "echo a#fragment\n",
])
def test_midword_hash_does_not_hide_actual_node_callee(tmp_path, prefix):
    (tmp_path / "stop.cjs").write_text("execSync('hermes gateway restart');\n")
    assert scan_gateway_lifecycle(
        prefix + "node stop.cjs", cwd=str(tmp_path), execution_context=known_context()
    ) == (True, None)


@pytest.mark.parametrize("prefix", [
    "echo 'declare -n R=PATH; set -k; trap payload DEBUG'; ",
    "echo '$((PATH=0)) $[HOME=0] ${a[PATH=0]} {PATH}>x'; ",
    "cat <<'TEXT'\n$((PATH=0))\n$[HOME=0]\n${a[PATH=0]}\n{PATH}>x\ntrap x DEBUG\nTEXT\n",
    "echo okay # ${HOME:=/synthetic}; node missing.cjs\n",
    "echo 'wait -np PATH'; ",
    'echo "\\$((PATH=0))"; ',
])
def test_reviewed_mutation_syntax_stays_inert_when_quoted(tmp_path, prefix):
    assert scan_gateway_lifecycle(
        prefix + "node -e 1", cwd=str(tmp_path), execution_context=known_context()
    ) == (False, None)
