"""FBL-370: bounded large public Node-source scans retain callee detection."""

import pytest

from cron import lifecycle_guard


def test_twenty_thousand_line_node_source_is_scanned(tmp_path):
    script = tmp_path / "public.cjs"
    script.write_text("#!/usr/bin/env node\n" + "const value = 1;\n" * 20_000)
    assert lifecycle_guard.scan_gateway_lifecycle("./public.cjs", cwd=str(tmp_path)) == (False, None)
    script.write_text(script.read_text() + "hermes gateway restart\n")
    assert lifecycle_guard.scan_gateway_lifecycle("./public.cjs", cwd=str(tmp_path)) == (True, None)


def test_default_line_boundary_and_plus_one_before_shlex(monkeypatch):
    assert lifecycle_guard.scan_gateway_lifecycle("\n" * 32767) == (False, None)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("over-budget root reached shlex")

    monkeypatch.setattr(lifecycle_guard.shlex, "shlex", forbidden)
    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle("\n" * 32768)
    assert unsafe and "budget" in refusal
    assert calls == []


@pytest.mark.parametrize("callee", ["suffixless", "module"])
def test_node_source_retains_nested_lifecycle_detection(tmp_path, callee):
    if callee == "suffixless":
        (tmp_path / "restart").write_text("#!/bin/sh\nhermes gateway restart\n")
        source = "require('child_process').execSync('./restart');\n"
    else:
        (tmp_path / "module.cjs").write_text("require('child_process').execSync('hermes gateway restart');\n")
        source = "require('./module.cjs');\n"
    (tmp_path / "app.cjs").write_text(source)
    assert lifecycle_guard.scan_gateway_lifecycle("./app.cjs", cwd=str(tmp_path)) == (True, None)


@pytest.mark.parametrize("prefix", [b"MZ=1\n", b"!<arch>\n", b"PK\x03\x04\n", b"\x1f\x8b\n"])
def test_magic_prefixed_text_remains_scannable(tmp_path, prefix):
    (tmp_path / "restart").write_bytes(prefix + b"hermes gateway restart\n")
    assert lifecycle_guard.scan_gateway_lifecycle("./restart", cwd=str(tmp_path)) == (True, None)


def test_shebang_wins_over_embedded_magic_and_nul(tmp_path):
    (tmp_path / "restart").write_bytes(b"#!/bin/sh\nMZ\x00\nhermes gateway restart\n")
    assert lifecycle_guard.scan_gateway_lifecycle("./restart", cwd=str(tmp_path)) == (True, None)
