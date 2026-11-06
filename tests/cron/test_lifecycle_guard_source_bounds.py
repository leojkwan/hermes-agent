"""FBL-370: large sources, exhausted work, and backend namespace isolation."""
import json
from pathlib import Path

import pytest

from tests.cron.lifecycle_context_fixtures import known_context

from cron import lifecycle_guard as guard
from cron.lifecycle_guard_references import SourceRead, read_local_source


@pytest.mark.parametrize("ending", ["safe", "eof", "helper"])
def test_large_typescript_shaped_source_is_fully_inspected(tmp_path, ending):
    source = "var value = 123456789; // benign compiler fixture padding\n" * 134000
    assert len(source.encode()) > 6 * 1024 * 1024
    if ending == "eof":
        source += "require('node:child_process').execSync('hermes gateway restart');\n"
    elif ending == "helper":
        source += "dynamicImport('./helper.cjs');\n"
    (tmp_path / "helper.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    (tmp_path / "compiler.cjs").write_text(source)
    stats = {}
    result = guard.scan_gateway_lifecycle("node compiler.cjs", cwd=str(tmp_path), scan_stats=stats, execution_context=known_context())
    assert result == (ending != "safe", None)
    assert stats["source_bytes"] >= len(source.encode())
    if ending == "helper":
        assert str(tmp_path / "helper.cjs") in stats["reached_paths"]


@pytest.mark.parametrize("constant,value,command,source,reason", [
    ("_MAX_REFERENCED_SCRIPT_BYTES", 8, "bash entry", "echo okay", "byte"),
    ("_MAX_LIFECYCLE_SOURCE_BYTES", 8, "bash entry", "echo okay", "byte"),
    ("_MAX_LIFECYCLE_SOURCE_LINES", 2, "bash entry", "one\ntwo\nthree", "budget"),
    ("_MAX_LIFECYCLE_SCAN_LINE_BYTES", 16, "bash entry", "é" * 9, "budget"),
    ("_MAX_LIFECYCLE_SCAN_TOKENS", 2, "echo one two", "", "token"),
    ("_MAX_LIFECYCLE_SCAN_PATHS", 0, "bash entry", "echo okay", "paths"),
    ("_LIFECYCLE_SCAN_SECONDS", 0, "echo okay", "", "deadline"),
])
def test_each_work_bound_has_a_named_refusal(monkeypatch, tmp_path, constant, value, command, source, reason):
    (tmp_path / "entry").write_text(source)
    monkeypatch.setattr(guard, constant, value)
    unsafe, refusal = guard.scan_gateway_lifecycle(command, cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and reason in refusal


def test_depth_overflow_is_named(tmp_path):
    for i in range(10):
        (tmp_path / f"s{i}.sh").write_text(f"bash s{i + 1}.sh\n")
    unsafe, reason = guard.scan_gateway_lifecycle("bash s0.sh", cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and "depth" in reason


def test_exact_source_and_plus_one_byte_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_MAX_REFERENCED_SCRIPT_BYTES", 8)
    (tmp_path / "entry").write_text("echo ok\n")
    assert guard.scan_gateway_lifecycle("bash entry", cwd=str(tmp_path), execution_context=known_context()) == (False, None)
    (tmp_path / "entry").write_text("echo ok\n ")
    unsafe, reason = guard.scan_gateway_lifecycle("bash entry", cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and "byte" in reason


class Remote:
    def __init__(self, files):
        self.files, self.calls = files, []

    def read_source(self, path, *, max_bytes, timeout):
        self.calls.append((path, max_bytes, timeout))
        text = self.files.get(path)
        return SourceRead("missing") if text is None else SourceRead("text", text, "remote:" + path)


def test_remote_package_projection_refuses_and_node_never_uses_local_namesake(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"check":"echo harmless"}}')
    remote = Remote({str(tmp_path / "package.json"): json.dumps({"scripts":{"check":"node entry.cjs"}}),
                     str(tmp_path / "entry.cjs"): "require('node:child_process').execSync('hermes gateway restart');\n"})
    unsafe, reason = guard.scan_gateway_lifecycle("npm run check", cwd=str(tmp_path), read_remote_script=remote,
                                        remote_namespace=True, execution_context=known_context())
    assert unsafe and "remote execution config capability unavailable" in reason
    assert remote.calls == []
    (tmp_path / "entry.cjs").write_text("const harmless=1;\n")
    assert guard.scan_gateway_lifecycle("node entry.cjs", cwd=str(tmp_path), read_remote_script=remote,
                                        remote_namespace=True, execution_context=known_context()) == (True, None)
    assert len(remote.calls) == 1
    assert all(0 < remaining <= 8 * 1024 * 1024 and 0 < timeout <= 5 for _, remaining, timeout in remote.calls)


def test_legacy_remote_callback_cannot_attest_new_metadata(tmp_path):
    unsafe, reason = guard.scan_gateway_lifecycle("npm run check", cwd=str(tmp_path),
        read_remote_script=lambda path: "echo okay", remote_namespace=True, execution_context=known_context())
    assert unsafe and "capability" in reason


def test_remote_timeout_refuses(tmp_path):
    class Slow(Remote):
        def read_source(self, *args, **kwargs):
            raise TimeoutError("synthetic timeout")
    unsafe, reason = guard.scan_gateway_lifecycle("node entry.cjs", cwd=str(tmp_path),
        read_remote_script=Slow({}), remote_namespace=True, execution_context=known_context())
    assert unsafe and "TimeoutError" in reason


def test_remote_read_overflow_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SCAN_REMOTE_READS", 1)
    remote = Remote({str(tmp_path / "entry"): "bash helper"})
    unsafe, reason = guard.scan_gateway_lifecycle("bash entry", cwd=str(tmp_path),
        read_remote_script=remote, remote_namespace=True, execution_context=known_context())
    assert unsafe and "remote reads" in reason
    assert len(remote.calls) == 1


def test_incomplete_mention_is_promoted_to_executed_scan(tmp_path):
    (tmp_path / "entry").write_text("x" * 65537)
    (tmp_path / "outer").write_text("python3 - <<'PY'\nprint('" + str(tmp_path / "entry") + "')\nPY\n")
    unsafe, reason = guard.scan_gateway_lifecycle("bash outer; bash entry", cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and "budget" in reason


def test_typed_binary_and_nul_text_results(tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"MZ\x00machine code")
    assert read_local_source(binary, max_bytes=100).status == "binary"
    binary.write_bytes(b"#!/bin/sh\nMZ\x00\nhermes gateway restart\n")
    result = read_local_source(binary, max_bytes=100)
    assert result.status == "text" and "hermes gateway restart" in result.text


def test_cloud_path_is_refused_before_open(monkeypatch):
    def forbidden(*args):
        raise AssertionError("opened a cloud placeholder")
    monkeypatch.setattr(guard.os, "open", forbidden)
    result = read_local_source(Path("/Users/example/Library/CloudStorage/vendor/entry"), max_bytes=100)
    assert result.status == "refused" and "cloud" in result.reason


@pytest.mark.parametrize("name", [".npmrc", ".env", ".netrc", ".git-credentials"])
def test_credential_configuration_is_refused_before_open(tmp_path, monkeypatch, name):
    path = tmp_path / name
    path.write_text("never inspect this credential configuration")

    def forbidden(*args, **kwargs):
        raise AssertionError("opened credential-bearing configuration")

    monkeypatch.setattr(guard.os, "open", forbidden)
    result = read_local_source(path, max_bytes=1024)
    assert result.status == "refused" and "credential" in result.reason


def test_total_source_bytes_exact_and_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SOURCE_BYTES", 16)
    (tmp_path / "a").write_text("echo ok\n")
    (tmp_path / "b").write_text("echo ok\n")
    assert guard.scan_gateway_lifecycle("bash a; bash b", cwd=str(tmp_path), execution_context=known_context()) == (False, None)
    (tmp_path / "b").write_text("echo ok\n ")
    unsafe, reason = guard.scan_gateway_lifecycle("bash a; bash b", cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and "byte" in reason


def test_source_lines_exact_and_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SOURCE_LINES", 3)
    (tmp_path / "a").write_text("one\ntwo\n")
    assert guard.scan_gateway_lifecycle("bash a", cwd=str(tmp_path), execution_context=known_context()) == (False, None)
    (tmp_path / "a").write_text("one\ntwo\n\n")
    unsafe, reason = guard.scan_gateway_lifecycle("bash a", cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and "budget" in reason


def test_token_allowance_exact_and_overflow(monkeypatch):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SCAN_TOKENS", 3)
    assert guard.scan_gateway_lifecycle("echo one two", execution_context=known_context()) == (False, None)
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SCAN_TOKENS", 2)
    unsafe, reason = guard.scan_gateway_lifecycle("echo one two", execution_context=known_context())
    assert unsafe and "token" in reason


def test_new_nested_input_still_consumes_token_allowance(monkeypatch):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SCAN_TOKENS", 3)
    unsafe, reason = guard.scan_gateway_lifecycle(
        "bash -c 'echo one two'", execution_context=known_context()
    )
    assert unsafe and "token" in reason


def test_nul_padding_counts_toward_total_read_work(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SOURCE_BYTES", 24)
    for name in ("a", "b"):
        (tmp_path / name).write_bytes(b"echo ok\n" + b"\0" * 8)
    unsafe, reason = guard.scan_gateway_lifecycle("bash a; bash b", cwd=str(tmp_path), execution_context=known_context())
    assert unsafe and "byte" in reason


def test_credential_target_alias_is_refused_before_open(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    target.write_text("credential configuration")
    alias = tmp_path / "helper"
    alias.symlink_to(target)

    def forbidden(*args, **kwargs):
        raise AssertionError("opened credential configuration through an alias")

    monkeypatch.setattr(guard.os, "open", forbidden)
    assert read_local_source(alias, max_bytes=1024).status == "refused"


@pytest.mark.parametrize("result", [SourceRead("unexpected"), SourceRead("text", "okay", byte_count=-1), SourceRead("text")])
def test_malformed_typed_backend_result_refuses(tmp_path, result):
    class Invalid(Remote):
        def read_source(self, *args, **kwargs):
            return result
    unsafe, reason = guard.scan_gateway_lifecycle("node entry.cjs", cwd=str(tmp_path),
        read_remote_script=Invalid({}), remote_namespace=True, execution_context=known_context())
    assert unsafe and "invalid backend" in reason
