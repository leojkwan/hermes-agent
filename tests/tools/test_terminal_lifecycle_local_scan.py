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

    env = SimpleNamespace(cwd=str(tmp_path), execute=forbidden)
    script = tmp_path / "app.cjs"
    script.write_text('const options = {\n "./absent": 1,\n};\n')
    assert _block(tmp_path, env, "local", "./app.cjs") is None
    assert calls == []


@pytest.mark.parametrize("env_type", ["ssh", "docker", "unknown", "", None, "LOCAL"])
def test_nonlocal_or_unknown_backend_retains_bounded_fallback(supervised, tmp_path, env_type):
    calls = []

    def absent(command):
        calls.append(command)
        return {"returncode": 1, "output": ""}

    env = SimpleNamespace(cwd=str(tmp_path), execute=absent)
    script = tmp_path / "app.cjs"
    script.write_text("\n".join(f"./missing{i}" for i in range(65)))
    result = json.loads(_block(tmp_path, env, env_type, "./app.cjs"))
    assert "remote reads" in result["error"]
    assert len(calls) == 64
