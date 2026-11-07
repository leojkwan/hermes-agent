"""FBL-370: qualified npm patches and conservative version-only inspection."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.lifecycle_context_fixtures import known_context, npm_context
from tools.environments.local_execution_context import EnvFact

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def project(tmp_path):
    context = npm_context(tmp_path)
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"probe": "node safe.cjs"}})
    )
    (tmp_path / "safe.cjs").write_text("console.log('safe');\n")
    (tmp_path / "stop.cjs").write_text(
        "require('child_process').execSync('hermes gateway restart');\n"
    )
    return tmp_path, context


def scan(project, command, context=None):
    directory, actual = project
    return scan_gateway_lifecycle(
        command, cwd=str(directory), execution_context=context or actual
    )


def set_version(project, version):
    metadata = Path(project[1].path.value).parent / "lib/node_modules/npm/package.json"
    metadata.write_text(json.dumps({"name": "npm", "version": version}))


@pytest.mark.parametrize("version", ["11.19.0", "11.19.1"])
@pytest.mark.parametrize("command", ["npm run probe", "npm -v", "npm --version"])
def test_reviewed_npm_versions_admit_script_and_version(project, version, command):
    set_version(project, version)
    assert scan(project, command) == (False, None)


@pytest.mark.parametrize("command", ["npm -v", "npm --version"])
def test_npm_version_has_no_script_or_package_requirement(project, command):
    set_version(project, "11.19.1")
    (project[0] / "package.json").unlink()
    assert scan(project, command) == (False, None)


@pytest.mark.parametrize("version", ["11.19.0", "11.19.1"])
def test_package_less_npm_version_keeps_cwd_config_private(project, version):
    set_version(project, version)
    (project[0] / "package.json").unlink()
    private_shell = "PRIVATE_SYNTHETIC_PACKAGE_LESS_SHELL"
    (project[0] / ".npmrc").write_text("script-shell=" + private_shell + "\n")
    unsafe, reason = scan(project, "npm -v")
    assert unsafe and "execution-affecting config" in reason
    assert private_shell not in reason


@pytest.mark.parametrize("version", ["11.19.2", "12.0.0", "11.19.1-extra", "", None])
@pytest.mark.parametrize("command", ["npm run probe", "npm -v"])
def test_unreviewed_npm_versions_refuse_actionably(project, version, command):
    set_version(project, version)
    unsafe, reason = scan(project, command)
    assert unsafe and "review" in reason and "11.19.0" in reason and "11.19.1" in reason


@pytest.mark.parametrize(
    "command",
    [
        "npm -vv",
        "npm --version=true",
        "npm --version=false",
        "npm -v -v",
        "npm --version --version",
        "npm -v probe",
        "npm -v run probe",
        "npm run probe -v",
        "npm -- -v",
        "npm --versions",
        "npm version",
        "npm --prefix . -v",
        "npm --node-options=--require=./stop.cjs -v",
    ],
)
def test_npm_version_does_not_hide_other_arguments(project, command):
    assert scan(project, command)[0]


@pytest.mark.parametrize("command", ["node -v", "node --version"])
def test_standalone_node_version(project, command):
    assert scan(project, command, known_context()) == (False, None)


@pytest.mark.parametrize(
    "command",
    [
        "node -vv",
        "node -v -v",
        "node --version --version",
        "node --version=true",
        "node -v safe.cjs",
        "node --version -- safe.cjs",
        "node -- -v",
        "node -e 1 -v",
        "node -v -e 1",
        "node -p 1 --version",
        "node -r ./stop.cjs -v",
        "node --version --require ./stop.cjs",
        "node --loader ./stop.cjs -v",
        "node --title harmless --version",
    ],
)
def test_node_version_does_not_hide_other_arguments(project, command):
    assert scan(project, command)[0]


@pytest.mark.parametrize(
    "command", ["node -v", "node --version", "npm -v", "npm run probe"]
)
@pytest.mark.parametrize(
    "fact",
    [
        EnvFact("unknown", reason="synthetic-unknown"),
        EnvFact("value", "--require ./stop.cjs"),
        EnvFact("value", "--import ./stop.cjs"),
        EnvFact("value", "--loader ./stop.cjs"),
        EnvFact("value", "--version"),
        EnvFact("value", "-e 1"),
    ],
)
def test_version_inspection_preserves_ambient_refusals(project, command, fact):
    set_version(project, "11.19.1")
    assert scan(project, command, replace(project[1], node_options=fact))[0]


@pytest.mark.parametrize("command", ["node -v", "node --version", "npm -v"])
def test_version_inspection_scans_safe_ambient_preloads(project, command):
    set_version(project, "11.19.1")
    context = replace(project[1], node_options=EnvFact("value", "--require ./safe.cjs"))
    assert scan(project, command, context) == (False, None)


@pytest.mark.parametrize("command", ["npm run probe", "npm -v", "npm --version"])
def test_patch_keeps_config_and_callee_guards(project, command):
    set_version(project, "11.19.1")
    (project[0] / ".npmrc").write_text("script-shell=private-synthetic-shell\n")
    unsafe, reason = scan(project, command)
    assert unsafe and "execution-affecting config" in reason
    assert "private-synthetic-shell" not in reason


@pytest.mark.parametrize("command", ["npm -v", "npm --version"])
@pytest.mark.parametrize(
    "changes",
    [
        {"home": EnvFact("unknown")},
        {"path": EnvFact("unknown")},
        {"npm_env_names": None},
        {"npm_path_safe": False},
        {"prefix": EnvFact("unknown")},
        {"destdir": EnvFact("unknown")},
        {"npm_env_names": frozenset({"npm_config_userconfig"})},
    ],
)
def test_npm_version_keeps_unknown_or_unsafe_routing_refused(project, command, changes):
    set_version(project, "11.19.1")
    assert scan(project, command, replace(project[1], **changes))[0]


def test_patch_still_scans_selected_script_callee(project):
    set_version(project, "11.19.1")
    (project[0] / "package.json").write_text('{"scripts":{"probe":"node stop.cjs"}}')
    assert scan(project, "npm run probe") == (True, None)
