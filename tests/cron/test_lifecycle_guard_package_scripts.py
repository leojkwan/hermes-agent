"""FBL-370: statically selected npm callees receive direct-command admission."""
import json

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


def package(tmp_path, scripts):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))


@pytest.mark.parametrize("command", ["npm run check", "npm run-script check", "npm -s run check",
    "npm run -s check", "npm --prefix=. run check", "npm run --prefix . check", "npm test"])
@pytest.mark.parametrize("hook", ["pre", "", "post"])
def test_hooks_are_inspected_without_npm(tmp_path, command, hook):
    name = "test" if command == "npm test" else "check"
    package(tmp_path, {name: "echo okay", hook + name: "bash helper.sh"})
    (tmp_path / "helper.sh").write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)


def test_nested_alias_and_forwarded_arguments(tmp_path):
    package(tmp_path, {"check": "npm run inner", "inner": "echo okay"})
    assert scan_gateway_lifecycle("npm run check", cwd=str(tmp_path)) == (False, None)
    (tmp_path / "evil.sh").write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle("npm run check -- ./evil.sh", cwd=str(tmp_path)) == (True, None)


@pytest.mark.parametrize("scripts,command,reason", [
    ({"check": "npm run check"}, "npm run check", "cycle"),
    ({"check": "npm run inner", "inner": "npm run check"}, "npm run check", "cycle"),
    ({}, "npm run absent", "missing"),
    ({"check": 12}, "npm run check", "malformed"),
    ({"check": "echo okay"}, "npm run $NAME", "dynamic"),
    ({"check": "echo okay"}, "npm --workspace web run check", "workspace"),
    ({"check": "echo okay"}, "npm --script-shell bash run check", "shell"),
    ({"check": "echo okay"}, "npm --userconfig custom run check", "config"),
    ({"check": "echo okay"}, "npm run", "selector"),
])
def test_unknowable_package_selection_refuses(tmp_path, scripts, command, reason):
    package(tmp_path, scripts)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and reason in refusal


def test_prefix_and_upward_package_lookup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    child = root / "sub"
    child.mkdir()
    package(root, {"check": "bash helper.sh"})
    (root / "helper.sh").write_text("hermes gateway restart\n")
    for cwd, command in [(child, "npm run check"), (tmp_path, "npm --prefix project run check")]:
        assert scan_gateway_lifecycle(command, cwd=str(cwd)) == (True, None)


def test_ignore_scripts_unknown_conservatively_scans_hooks(tmp_path):
    package(tmp_path, {"precheck": "hermes gateway restart", "check": "echo okay"})
    assert scan_gateway_lifecycle("npm run check --ignore-scripts", cwd=str(tmp_path)) == (True, None)


@pytest.mark.parametrize("dangling", [False, True])
def test_selected_ancestor_bin_and_same_name(tmp_path, dangling):
    package(tmp_path, {"check": "mycheck"})
    bins = tmp_path / "node_modules" / ".bin"
    bins.mkdir(parents=True)
    (tmp_path / "mycheck").write_text("echo benign\n")
    selected = bins / "mycheck"
    if dangling:
        selected.symlink_to(tmp_path / "absent")
    else:
        selected.write_text("hermes gateway restart\n")
    unsafe, refusal = scan_gateway_lifecycle("npm run check", cwd=str(tmp_path))
    assert unsafe
    assert ("dangling" in refusal) if dangling else refusal is None


def test_symlink_bin_resolves_helpers_from_target_directory(tmp_path):
    package(tmp_path, {"check": "mycheck"})
    bins = tmp_path / "node_modules" / ".bin"
    bins.mkdir(parents=True)
    target = tmp_path / "node_modules" / "tool" / "bin"
    target.mkdir(parents=True)
    (target / "entry").write_text("#!/usr/bin/env node\nrequire('../helper.cjs');\n")
    (target.parent / "helper.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    (bins / "mycheck").symlink_to(target / "entry")
    assert scan_gateway_lifecycle("npm run check", cwd=str(tmp_path)) == (True, None)


def test_malformed_metadata_refuses(tmp_path):
    (tmp_path / "package.json").write_text("{oops")
    unsafe, reason = scan_gateway_lifecycle("npm run check", cwd=str(tmp_path))
    assert unsafe and "metadata" in reason


def test_unrelated_bare_command_keeps_existing_behavior(tmp_path):
    assert scan_gateway_lifecycle("ordinary-tool --check", cwd=str(tmp_path)) == (False, None)


def test_explicit_dangling_bin_refuses(tmp_path):
    bins = tmp_path / "node_modules" / ".bin"
    bins.mkdir(parents=True)
    (bins / "tool").symlink_to(tmp_path / "absent")
    unsafe, reason = scan_gateway_lifecycle("./node_modules/.bin/tool", cwd=str(tmp_path))
    assert unsafe and "dangling" in reason


def test_nul_in_package_metadata_is_malformed(tmp_path):
    (tmp_path / "package.json").write_bytes(b'{"scripts":{"check":"echo okay"}}\0')
    unsafe, reason = scan_gateway_lifecycle("npm run check", cwd=str(tmp_path))
    assert unsafe and "malformed" in reason
