"""FBL-370 npm configuration admission, using only synthetic owned files."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from cron import lifecycle_guard as guard
from cron.lifecycle_guard_npm import NpmProjection, parse_selection
from cron.lifecycle_guard_references import ReferenceScanner, read_local_source
from tests.cron.lifecycle_context_fixtures import known_context, npm_context
from tools.environments.local_execution_context import EnvFact

SECRET = "SYNTHETIC_AUTH_MUST_NEVER_APPEAR_370"
pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def project(tmp_path):
    context = npm_context(tmp_path)
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"check": "npm run inner", "inner": "node -e 1"}})
    )
    prefix = Path(context.path.value).parent
    root = prefix / "lib/node_modules/npm"
    layers = [
        root / "npmrc",
        tmp_path / ".npmrc",
        Path(context.home.value) / ".npmrc",
        prefix / "etc/npmrc",
    ]
    for path in layers:
        path.write_text(
            "registry=https://example.invalid/\n//example.invalid/:_authToken="
            + SECRET
            + "\n"
        )
    return tmp_path, context, layers


def scan(project, command="npm run check", *, context=None, stats=None):
    directory, actual, _ = project
    return guard.scan_gateway_lifecycle(
        command,
        cwd=str(directory),
        execution_context=context or actual,
        scan_stats=stats,
    )


def test_neutral_four_layers_and_nested_node_never_execute_or_expose_values(
    project, monkeypatch, capsys, caplog
):
    def forbidden(*args, **kwargs):
        raise AssertionError("projection attempted process execution")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    stats = {}
    assert scan(project, stats=stats) == (False, None)
    directory, context, _ = project
    scanner = ReferenceScanner(guard._LifecycleScanBudget(), None, False, context)
    selection = parse_selection(("npm", "run", "check"), str(directory))
    projection = NpmProjection(
        scanner, selection, ("npm", "run", "check"), str(directory), None
    )
    facts = projection.project()
    assert facts.status == "known" and facts.local_prefix == str(directory.resolve())
    assert SECRET not in repr((facts, projection.children, stats, scanner.reads))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + caplog.text
    assert not any(
        path.endswith((".npmrc", "/npmrc")) for path in stats["reached_paths"]
    )


@pytest.mark.parametrize("location", ["project", "npm"])
@pytest.mark.parametrize(
    "payload",
    [
        b'{"scripts":{"check":"echo okay"}}\0',
        ("{" + SECRET).encode(),
        json.dumps([SECRET]).encode(),
    ],
    ids=["nul", "invalid-json", "non-object"],
)
def test_malformed_metadata_keeps_fixed_private_classification(
    project, location, payload, capsys, caplog
):
    directory, context, _ = project
    root = (
        directory
        if location == "project"
        else Path(context.path.value).parent / "lib/node_modules/npm"
    )
    (root / "package.json").write_bytes(payload)
    assert scan(project) == (True, "npm projection: malformed package metadata")
    captured = capsys.readouterr()
    diagnostics = captured.out + captured.err + caplog.text
    assert str(directory) not in diagnostics
    assert SECRET not in diagnostics


@pytest.mark.parametrize("layer", range(4))
@pytest.mark.parametrize(
    "key",
    [
        "node-options",
        "NODE_OPTIONS",
        "script_shell",
        "userconfig",
        "globalconfig",
        "prefix",
        "global",
        "location",
        "workspace",
        "workspaces",
        "include_workspace_root",
        "onload-script",
    ],
)
def test_any_execution_key_refuses_without_interpreting_secret(project, layer, key):
    project[2][layer].write_text(key + "=" + SECRET + "\n")
    unsafe, reason = scan(project)
    assert unsafe and "npm projection" in reason
    assert SECRET not in reason


@pytest.mark.parametrize("value", ["", "false", "--require ./stop.cjs"])
def test_config_node_options_refuses_even_empty_or_overridden(project, value):
    project[2][1].write_text("node-options=" + value + "\n")
    project[2][2].write_text("node-options=--require ./other.cjs\n")
    unsafe, reason = scan(project)
    assert unsafe and "execution-affecting config" in reason


@pytest.mark.parametrize(
    "name",
    [
        "npm_config_node_options",
        "NPM_CONFIG_Node_Options",
        "npm_config_node-options",
        "npm_config_userconfig",
        "npm_config_globalconfig",
        "npm_config_prefix",
        "npm_config_workspace",
        "npm_config_onload_script",
    ],
)
def test_ambient_relevant_names_are_never_treated_as_generated(project, name):
    context = replace(project[1], npm_env_names=frozenset({name.lower()}))
    unsafe, reason = scan(project, context=context)
    assert unsafe and "environment config" in reason
    assert SECRET not in reason


@pytest.mark.parametrize(
    "command",
    [
        "npm --userconfig /never/read run check",
        "npm --globalconfig /never/read run check",
        "npm --node-options=private run check",
        "npm --script-shell bash run check",
        "npm --global run check",
        "npm --location=global run check",
        "npm -w app run check",
        "npm --prefix '$DYNAMIC' run check",
    ],
)
def test_cli_routing_refuses_before_config_read(project, command, monkeypatch):
    import cron.lifecycle_guard_npm as npm

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported selector followed a config path")

    monkeypatch.setattr(npm, "_read_config", forbidden)
    unsafe, reason = scan(project, command)
    assert unsafe and ("selection" in reason or "dynamic" in reason)


@pytest.mark.parametrize(
    "field,fact",
    [
        ("home", EnvFact("unknown")),
        ("path", EnvFact("unknown")),
        ("home", EnvFact("value", "")),
        ("path", EnvFact("value", "/bin:")),
        ("path", EnvFact("value", "relative")),
        ("prefix", EnvFact("value", "")),
        ("destdir", EnvFact("unknown")),
    ],
)
def test_unknown_or_unmodeled_facts_refuse(project, field, fact):
    assert scan(project, context=replace(project[1], **{field: fact}))[0]


@pytest.mark.parametrize(
    "line",
    [
        '"node-options"=private',
        "'node-options'=private",
        "[section]",
        "node-options[]=private",
        "${NAME}=private",
        "node\\-options=private",
        "bad key=private",
        "bare-key",
        "nødé-options=private",
    ],
)
def test_unsupported_ini_syntax_refuses_with_fixed_reason(project, line):
    project[2][1].write_text(line + SECRET + "\n")
    unsafe, reason = scan(project)
    assert unsafe and "key syntax" in reason and SECRET not in reason


def test_unrelated_secret_values_are_discarded_without_decoding(project):
    project[2][1].write_bytes(
        b"# comment\r\n; comment\r\n_authToken=\xff${SECRET}\\#\\;\nregistry=https://example.invalid/#unused\n"
    )
    assert scan(project) == (False, None)


def test_builtin_prefix_selects_global_config_and_propagates_to_nested_npm(project):
    directory, _, layers = project
    prefix = directory / "builtin-prefix"
    (prefix / "etc").mkdir(parents=True)
    layers[0].write_text("prefix=" + str(prefix) + "\n")
    selected = prefix / "etc/npmrc"
    selected.write_text("registry=https://example.invalid/\n")
    assert scan(project) == (False, None)
    selected.write_text("node_options=" + SECRET + "\n")
    unsafe, reason = scan(project)
    assert unsafe and "config key" in reason and SECRET not in reason


def test_explicit_prefix_selects_exact_package_and_global_layer(project):
    directory, _, layers = project
    target = directory / "selected"
    (target / "etc").mkdir(parents=True)
    (target / "package.json").write_text('{"scripts":{"check":"node -e 1"}}')
    layers[3].write_text("node-options=unsupported-old-path\n")
    command = "npm --prefix selected run check"
    assert scan(project, command) == (False, None)
    (target / "etc/npmrc").write_text("script-shell=" + SECRET)
    unsafe, reason = scan(project, command)
    assert unsafe and "config key" in reason and SECRET not in reason


def test_nested_changed_prefix_retains_exported_globalconfig_and_checks_new_project(
    project,
):
    directory, _, _ = project
    target = directory / "selected"
    (target / "etc").mkdir(parents=True)
    (target / "package.json").write_text('{"scripts":{"inner":"node -e 1"}}')
    (directory / "package.json").write_text(
        '{"scripts":{"check":"npm --prefix selected run inner"}}'
    )
    # The parent's already evaluated globalconfig is exported as an env selector.
    (target / "etc/npmrc").write_text("node-options=not-selected-by-nested-npm\n")
    assert scan(project) == (False, None)
    (target / ".npmrc").write_text("node-options=" + SECRET)
    unsafe, reason = scan(project)
    assert unsafe and "config key" in reason and SECRET not in reason


@pytest.mark.parametrize(
    "payload",
    [
        "env npm_config_prefix=/changed npm run inner",
        "env -u npm_config_prefix npm run inner",
        "env npm_config_globalconfig=/changed npm run inner",
        "HOME=/changed npm run inner",
    ],
)
def test_changed_child_context_cannot_reuse_generated_routing(project, payload):
    (project[0] / "package.json").write_text(
        json.dumps({"scripts": {"check": payload, "inner": "node -e 1"}})
    )
    unsafe, reason = scan(project)
    assert unsafe and "environment config" in reason


def test_startup_preload_uses_invocation_cwd_and_child_inherits_options(project):
    directory, context, _ = project
    (directory / "stop.cjs").write_text(
        "require('child_process').execSync('hermes gateway restart');\n"
    )
    dangerous = replace(context, node_options=EnvFact("value", "--require ./stop.cjs"))
    assert scan(project, context=dangerous) == (True, None)
    (directory / "stop.cjs").write_text("const value=1;\n")
    assert scan(project, context=dangerous) == (False, None)


@pytest.mark.parametrize("implicit", [True, False])
def test_workspace_ancestry_refuses(project, implicit):
    directory, _, _ = project
    child = directory / "child"
    child.mkdir()
    (child / "package.json").write_text('{"scripts":{"check":"echo harmless"}}')
    (directory / "package.json").write_text('{"workspaces":["child"]}')
    command = "npm run check" if implicit else "npm --prefix . run check"
    unsafe, reason = guard.scan_gateway_lifecycle(
        command, cwd=str(child), execution_context=project[1]
    )
    assert unsafe and "workspace" in reason


def test_nearest_node_modules_without_package_does_not_skip_to_ancestor(project):
    child = project[0] / "child"
    (child / "node_modules").mkdir(parents=True)
    unsafe, reason = guard.scan_gateway_lifecycle(
        "npm run check", cwd=str(child), execution_context=project[1]
    )
    assert unsafe and "node_modules boundary" in reason


@pytest.mark.parametrize(
    "kind", ["symlink", "dangling", "oversize", "directory", "sqlite", "nul"]
)
def test_unsafe_config_files_refuse_without_values(project, kind):
    path = project[2][1]
    path.unlink()
    create = {
        "symlink": lambda: path.symlink_to(project[2][2]),
        "dangling": lambda: path.symlink_to(project[0] / "missing"),
        "directory": path.mkdir,
        "oversize": lambda: path.write_bytes(b"#" * 65537),
        "sqlite": lambda: path.write_bytes(b"SQLite format 3\0" + SECRET.encode()),
        "nul": lambda: path.write_bytes(b"registry=\0" + SECRET.encode()),
    }
    create[kind]()
    unsafe, reason = scan(project)
    assert unsafe and "npm projection" in reason and SECRET not in reason


def test_cloud_config_refused_before_open(project, monkeypatch):
    original = guard._on_cloud_path
    config = project[2][1]
    monkeypatch.setattr(
        guard, "_on_cloud_path", lambda path: Path(path) == config or original(path)
    )
    unsafe, reason = scan(project)
    assert unsafe and "cloud config" in reason


def test_generic_credential_reader_stays_denied(project):
    result = read_local_source(project[2][1], max_bytes=65536)
    assert result.status == "refused" and result.text is None
    assert SECRET not in repr(result)


def test_unreviewed_npm_or_node_shim_refuses(project):
    prefix = Path(project[1].path.value).parent
    metadata = prefix / "lib/node_modules/npm/package.json"
    metadata.write_text('{"name":"npm","version":"12.0.0"}')
    assert "version" in scan(project)[1]
    metadata.write_text('{"name":"npm","version":"11.19.0"}')
    (prefix / "bin/node").write_text("#!/bin/sh\nexec some-node\n")
    assert "unsupported node" in scan(project)[1]


def test_node_alias_to_unrelated_binary_is_not_attested(project):
    node = Path(project[1].path.value) / "node"
    unrelated = node.with_name("unrelated-binary")
    node.rename(unrelated)
    node.symlink_to(unrelated)
    unsafe, reason = scan(project)
    assert unsafe and "unsupported node" in reason


def test_npm_config_work_charges_shared_scan_bounds(project, monkeypatch):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SOURCE_LINES", 3)
    unsafe, reason = scan(project)
    assert unsafe and "budget" in reason


def test_blank_config_lines_cannot_escape_line_budget(project):
    project[2][1].write_bytes(b"\n" * 4096)
    unsafe, reason = scan(project)
    assert unsafe and "line budget" in reason


@pytest.mark.parametrize(
    "command", ["Path=/synthetic npm run check", "env pAtH= npm run check"]
)
def test_case_variant_path_overlay_refuses(project, command):
    unsafe, reason = scan(project, command)
    assert unsafe and "PATH aliases" in reason


def test_missing_path_alias_attestation_is_unknown(project):
    unsafe, reason = scan(project, context=replace(project[1], npm_path_safe=False))
    assert unsafe and "PATH aliases" in reason


def test_unreadable_config_has_fixed_private_error(project, monkeypatch):
    original = os.open
    selected = project[2][1]

    def unavailable(path, *args, **kwargs):
        if Path(path) == selected:
            raise PermissionError(SECRET)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", unavailable)
    unsafe, reason = scan(project)
    assert unsafe and "config unavailable" in reason and SECRET not in reason


def test_live_sqlite_config_is_refused_before_raw_open(project, monkeypatch):
    from hermes_cli.sqlite_safe_read import connect_tracked

    selected = project[2][1]
    selected.unlink()
    connection = connect_tracked(selected)
    connection.execute("CREATE TABLE private_data (value TEXT)")
    original = os.open

    def no_raw_sqlite(path, *args, **kwargs):
        assert Path(path) != selected, "live SQLite was opened as a config file"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_raw_sqlite)
    try:
        unsafe, reason = scan(project)
        assert unsafe and "live SQLite" in reason
    finally:
        connection.close()


@pytest.mark.parametrize(
    "limit,value", [("deadline", 0), ("paths_remaining", 0), ("bytes_remaining", 4)]
)
def test_private_projection_uses_existing_budget(project, limit, value):
    directory, context, _ = project
    budget = guard._LifecycleScanBudget()
    setattr(budget, limit, value)
    scanner = ReferenceScanner(budget, None, False, context)
    selection = parse_selection(("npm", "run", "check"), str(directory))
    result = NpmProjection(
        scanner, selection, ("npm", "run", "check"), str(directory), None
    ).project()
    assert result.status == "refused" and "budget" in result.reason
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "command",
    [
        "bash -o monitor -lc 'node -e 1'",
        "bash +o monitor -ic 'node -e 1'",
        "bash --rcfile /synthetic-init -ic 'node -e 1'",
    ],
)
def test_shell_option_operands_do_not_hide_initialization(tmp_path, command):
    unsafe, reason = guard.scan_gateway_lifecycle(
        command, cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and "shell-initialization-context-unavailable" in reason


def test_login_shebang_does_not_reuse_foreground_facts(tmp_path):
    (tmp_path / "entry").write_text("#!/usr/bin/env -S bash -l\nnode -e 1\n")
    unsafe, reason = guard.scan_gateway_lifecycle(
        "./entry", cwd=str(tmp_path), execution_context=known_context()
    )
    assert unsafe and "shell-initialization-context-unavailable" in reason


def test_shell_script_argument_is_not_a_command_payload(tmp_path):
    (tmp_path / "entry").write_text("echo okay\n")
    assert guard.scan_gateway_lifecycle(
        "bash entry -c 'node -e 1'", cwd=str(tmp_path)
    ) == (False, None)
