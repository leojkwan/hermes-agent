"""FBL-370 execution context overlays and npm startup inspection."""

from dataclasses import replace

import pytest

from cron import lifecycle_guard as guard
from cron.lifecycle_guard_references import ReferenceScanner
from tests.cron.lifecycle_context_fixtures import known_context
from tools.environments.local_execution_context import EnvFact, ExecutionContext


@pytest.mark.parametrize(
    "name",
    ["NODE_OPTIONS", "HOME", "PATH", "PREFIX", "DESTDIR", "NPM_CONFIG_Node_Options"],
)
@pytest.mark.parametrize(
    "setter",
    [
        "{name}=changed",
        "export {name}=changed",
        "unset {name}",
        "declare -x {name}=changed",
        "readonly {name}=changed",
        "local {name}=changed",
    ],
)
def test_earlier_fact_mutations_refuse_later_node(tmp_path, name, setter):
    unsafe, reason = guard.scan_gateway_lifecycle(
        setter.format(name=name) + "; node -e 1",
        cwd=str(tmp_path),
        execution_context=known_context(),
    )
    assert unsafe and "execution context changed earlier" in reason


@pytest.mark.parametrize(
    "setter",
    [
        "source helper",
        ". helper",
        "eval 'echo harmless'",
        "set -a",
        "set -ax",
        "export $NAME=value",
    ],
)
def test_unmodeled_shell_mutations_refuse_later_node(tmp_path, setter):
    unsafe, reason = guard.scan_gateway_lifecycle(
        setter + "; node -e 1", cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and "execution context changed earlier" in reason


@pytest.mark.parametrize(
    "wrapper",
    ["sudo", "su", "runuser", "pkexec", "doas", "nsenter", "unshare", "systemd-run"],
)
def test_environment_changing_wrappers_never_inherit_context(tmp_path, wrapper):
    unsafe, reason = guard.scan_gateway_lifecycle(
        wrapper + " node -e 1", cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and "environment-changing-wrapper" in reason


def test_default_context_is_unknown(tmp_path):
    unsafe, reason = guard.scan_gateway_lifecycle("node -e 1", cwd=str(tmp_path))
    assert unsafe and "execution-context-unavailable" in reason
    assert guard.scan_gateway_lifecycle(
        "node -e 1", cwd=str(tmp_path), execution_context=known_context()
    ) == (False, None)


def test_all_fact_names_and_npm_names_share_invocation_parser(tmp_path):
    scanner = ReferenceScanner(
        guard._LifecycleScanBudget(), None, False, ExecutionContext.unknown()
    )
    argv, context = scanner.invocation_context(
        [
            "env",
            "-i",
            "HOME=/home",
            "PATH=/bin",
            "PREFIX=",
            "DESTDIR=/dest",
            "NPM_CONFIG_Node_Options=private",
            "node",
            "-e",
            "1",
        ]
    )
    assert argv == ["node", "-e", "1"]
    assert context.home == EnvFact("value", "/home")
    assert context.path == EnvFact("value", "/bin")
    assert context.prefix == EnvFact("value", "")
    assert context.destdir == EnvFact("value", "/dest")
    assert context.node_options.status == "absent"
    assert context.npm_env_names == frozenset({"npm_config_node_options"})
    scanner.options = context
    _, updated = scanner.invocation_context(
        ["env", "--unset=HOME", "PATH=$PATH", "node", "-e", "1"]
    )
    assert updated.home.status == "absent"
    assert updated.path.status == "unknown"


def test_npm_preloads_are_inspected_before_projection_refusal(tmp_path):
    (tmp_path / "stop.cjs").write_text(
        "require('child_process').execSync('hermes gateway restart');\n"
    )
    assert guard.scan_gateway_lifecycle(
        "npm test",
        cwd=str(tmp_path),
        execution_context=known_context("--require ./stop.cjs"),
    ) == (True, None)
    unsafe, reason = guard.scan_gateway_lifecycle(
        "npm test", cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and "npm projection" in reason


def test_case_alias_unset_cannot_erase_unmodeled_npm_presence():
    scanner = ReferenceScanner(
        guard._LifecycleScanBudget(),
        None,
        False,
        replace(known_context(), npm_env_names=frozenset({"npm_config_node_options"})),
    )
    _, context = scanner.invocation_context(
        ["env", "-u", "npm_config_node_options", "node", "-e", "1"]
    )
    assert context.npm_env_names == frozenset({"npm_config_node_options"})


@pytest.mark.parametrize("shell", ["bash -lc", "bash -ic", "sh --login -c"])
def test_login_shell_payload_does_not_reuse_foreground_facts(tmp_path, shell):
    unsafe, reason = guard.scan_gateway_lifecycle(
        shell + " 'node -e 1'", cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and "shell-initialization-context-unavailable" in reason


def test_clearing_node_options_does_not_erase_other_inline_mutations(tmp_path):
    unsafe, reason = guard.scan_gateway_lifecycle(
        "PATH=changed; NODE_OPTIONS= node -e 1",
        cwd=str(tmp_path),
        execution_context=known_context(),
    )
    assert unsafe and "execution context changed earlier" in reason


@pytest.mark.parametrize(
    "command", ["sudo -u nobody env -u NODE_OPTIONS node -e 1", "sudo env -i node -e 1"]
)
def test_env_overlay_cannot_attest_identity_changing_wrapper(tmp_path, command):
    unsafe, reason = guard.scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and "environment-changing-wrapper" in reason


@pytest.mark.parametrize(
    "setter",
    [
        "NODE_OPTIONS[0]=value",
        "declare -ax NODE_OPTIONS=(value)",
        "unset NODE_OPTIONS[0]",
        "set -o allexport",
    ],
)
def test_array_and_allexport_mutations_refuse_later_node(tmp_path, setter):
    unsafe, reason = guard.scan_gateway_lifecycle(
        setter + "; node -e 1", cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and reason is not None


def test_env_assignment_names_are_not_limited_to_shell_identifiers(tmp_path):
    scanner = ReferenceScanner(
        guard._LifecycleScanBudget(), None, False, known_context()
    )
    argv, context = scanner.invocation_context(
        ["env", "npm_config_node-options=private", "node", "-e", "1"]
    )
    assert argv == ["node", "-e", "1"]
    assert context.npm_env_names == frozenset({"npm_config_node-options"})
    (tmp_path / "stop.cjs").write_text(
        "require('child_process').execSync('hermes gateway restart');\n"
    )
    assert guard.scan_gateway_lifecycle(
        "env npm_config_node-options=private node stop.cjs",
        cwd=str(tmp_path),
        execution_context=known_context(),
    ) == (True, None)


def test_dynamic_env_assignment_name_cannot_hide_options(tmp_path):
    unsafe, reason = guard.scan_gateway_lifecycle(
        "env $NAME=value node -e 1",
        cwd=str(tmp_path),
        execution_context=known_context(),
    )
    assert unsafe and "unresolved environment assignment name" in reason
