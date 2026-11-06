"""FBL-370: effective-local preflight never dispatches backend shell reads."""

import json
from types import SimpleNamespace

import pytest

from tools.terminal_tool_guards import gateway_lifecycle_block


@pytest.fixture
def supervised(monkeypatch):
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd", lambda _key: None)


def _block(tmp_path, env, env_type, command):
    return gateway_lifecycle_block(
        command=command, env=env, env_type=env_type, cwd=str(tmp_path),
        workdir=None, session_key="local-scan-test",
    )


def test_local_source_labels_do_not_dispatch_backend_reads(supervised, tmp_path):
    calls = []

    def forbidden(command):
        calls.append(command)
        raise AssertionError("local preflight dispatched a backend command")

    env = SimpleNamespace(cwd=str(tmp_path), env_type="local", execute=forbidden)
    script = tmp_path / "app.cjs"
    script.write_text('const options = {\n "./absent": 1,\n};\n')
    assert _block(tmp_path, env, "local", "./app.cjs") is None
    assert calls == []


@pytest.mark.parametrize("env_type", ["ssh", "docker", "unknown", "", None, "LOCAL"])
def test_nonlocal_or_unknown_backend_retains_bounded_fallback(supervised, tmp_path, env_type):
    calls = []

    def absent(command):
        calls.append(command)
        if "app.cjs" in command:
            return {"returncode": 0, "output": "\n".join(f"./missing{i}" for i in range(65))}
        return {"returncode": 1, "output": ""}

    env = SimpleNamespace(cwd=str(tmp_path), execute=absent)
    script = tmp_path / "app.cjs"
    script.write_text("\n".join(f"./missing{i}" for i in range(65)))
    result = json.loads(_block(tmp_path, env, env_type, "./app.cjs"))
    assert "remote reads" in result["error"]
    assert len(calls) == 64


@pytest.mark.parametrize("live_type", ["ssh", "docker", "unknown", "", None])
def test_effective_local_retains_remote_lifecycle_coverage(supervised, tmp_path, live_type):
    calls = []

    def remote_read(command):
        calls.append(command)
        return {"returncode": 0, "output": "hermes gateway restart\n"}

    env = SimpleNamespace(cwd=str(tmp_path), env_type=live_type, execute=remote_read)
    result = json.loads(_block(tmp_path, env, "local", "./remote-only"))
    assert "cannot restart, stop, or uninstall the gateway" in result["error"]
    assert len(calls) == 1


def test_missing_live_backend_tag_retains_remote_lifecycle_coverage(supervised, tmp_path):
    env = SimpleNamespace(
        cwd=str(tmp_path),
        execute=lambda command: {"returncode": 0, "output": "hermes gateway restart\n"},
    )
    result = json.loads(_block(tmp_path, env, "local", "./remote-only"))
    assert "cannot restart, stop, or uninstall the gateway" in result["error"]


def test_local_magic_prefixed_text_helper_is_scanned_without_backend(supervised, tmp_path):
    calls = []

    def forbidden(command):
        calls.append(command)
        raise AssertionError("local preflight dispatched a backend command")

    env = SimpleNamespace(cwd=str(tmp_path), env_type="local", execute=forbidden)
    (tmp_path / "restart").write_text("MZ=1\nhermes gateway restart\n")
    result = json.loads(_block(tmp_path, env, "local", "./restart"))
    assert "cannot restart, stop, or uninstall the gateway" in result["error"]
    assert calls == []


def test_local_node_fact_and_unknown_context_never_execute(supervised, tmp_path):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("local preflight dispatched a backend command")

    (tmp_path / "entry.cjs").write_text("const value = 1;\n")
    (tmp_path / "helper.cjs").write_text("require('node:child_process').execSync('hermes gateway restart');\n")
    env = SimpleNamespace(cwd=str(tmp_path), env_type="local", execute=forbidden)
    result = json.loads(_block(tmp_path, env, "local", "node entry.cjs"))
    assert "execution context" in result["error"]
    assert _block(tmp_path, env, "local", "NODE_OPTIONS='' node entry.cjs") is None
    env.lifecycle_node_options = "--require ./helper.cjs"
    result = json.loads(_block(tmp_path, env, "local", "node entry.cjs"))
    assert "cannot restart, stop, or uninstall" in result["error"]
    assert calls == []
