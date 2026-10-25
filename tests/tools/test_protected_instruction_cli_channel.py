"""Protected-instruction approval fails closed at once, without asking anyone, where nobody can answer.

``hermes chat -q`` (how kanban spawns every worker), cron and unattended platforms can have the CLI approval
callback registered on the agent thread. The gate used to ask it, block for the full approvals timeout, then
report "timed out" — which reads as a human ignoring a prompt nobody saw. The interactive approve/deny paths
are covered in test_file_write_safety.py; the real CLI modal in tests/hermes_cli/test_cli_approval_ui.py.
"""

import json

import pytest


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    import tools.file_tools_write_guards as guards
    from tools.terminal_tool import set_approval_callback
    monkeypatch.setattr(guards, "_protected_instruction_config", lambda: (True, []))
    for name in ("HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION", "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    set_approval_callback(None)
    yield
    set_approval_callback(None)


@pytest.mark.parametrize("env", [
    {"HERMES_SINGLE_QUERY_SESSION": "1"},
    {"HERMES_CRON_SESSION": "1"},
    {"HERMES_SESSION_PLATFORM": "webhook"},
], ids=["single_query", "cron", "webhook"])
def test_registered_callback_is_never_asked_without_a_user(tmp_path, monkeypatch, env):
    from tools.terminal_tool import set_approval_callback
    from tools.file_tools import write_file_tool

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    asked = []
    set_approval_callback(lambda command, description, **kw: asked.append(command) or "once")
    target = tmp_path / "SOUL.md"
    res = json.loads(write_file_tool(str(target), "content"))

    assert asked == []
    assert "no interactive user or gateway is present" in res["error"]
    assert "timed out" not in res["error"]
    assert not target.exists()


@pytest.mark.parametrize("mode", ["approve", "deny"])
def test_single_query_mode_never_auto_approves_this_gate(tmp_path, monkeypatch, mode):
    """``approvals.single_query_mode`` governs the dangerous-command gate only: a protected-instruction write
    always needs a live human."""
    from tools import approval_context
    from tools.file_tools import write_file_tool
    from tools.terminal_tool import set_approval_callback

    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setattr(approval_context, "_get_single_query_approval_mode", lambda: mode)
    asked = []
    set_approval_callback(lambda command, description, **kw: asked.append(command) or "once")
    target = tmp_path / "SOUL.md"
    res = json.loads(write_file_tool(str(target), "content"))

    assert asked == []
    assert "BLOCKED" in res["error"]
    assert not target.exists()


@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "notify_failed", "denied"])
def test_gateway_missing_approval_attribution(tmp_path, monkeypatch, outcome):
    from tools import approval, approval_context
    from tools.file_tools import write_file_tool

    timeout = 0.05 if outcome == "timeout" else 60
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(f"approvals:\n  mode: manual\n  timeout: {timeout}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    session_key = "protected-attribution"
    token = approval_context.set_current_session_key(session_key)
    notified = []

    def notify(data):
        notified.append(data)
        assert data["allow_permanent"] is False
        assert data["allow_session"] is False
        if outcome == "notify_failed":
            raise RuntimeError("synthetic notification failure")
        if outcome == "cancelled":
            approval.withdraw_gateway_approval(session_key, data["request_id"], "synthetic withdrawal")
        elif outcome == "denied":
            approval.resolve_gateway_approval(session_key, "deny", request_id=data["request_id"])

    approval.register_gateway_notify(session_key, notify)
    target = tmp_path / "AGENTS.md"
    try:
        result = json.loads(write_file_tool(str(target), "synthetic content"))
    finally:
        approval.unregister_gateway_notify(session_key)
        approval_context.reset_current_session_key(token)

    assert len(notified) == 1, result
    assert not target.exists()
    assert not approval.has_blocking_approval(session_key)
    if outcome == "denied":
        assert result["error"] == (
            "BLOCKED: write to protected agent-instruction file(s) (AGENTS.md) was denied by the user. "
            "The user has NOT consented to this write. Do NOT retry it or attempt the same edit via "
            "another path (terminal, execute_code, etc.).")
    else:
        cause = {"cancelled": "synthetic withdrawal", "timeout": "timed out without a user response",
                 "notify_failed": "approval request could not be delivered"}[outcome]
        assert cause in result["error"]
        assert "No runtime approval was obtained" in result["error"]
        assert "NOT consented" not in result["error"]
        assert "Do NOT retry" in result["error"]


@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "denied"])
def test_cli_missing_approval_attribution(tmp_path, monkeypatch, outcome):
    from tools.file_tools import write_file_tool
    from tools.terminal_tool import set_approval_callback

    def callback(command, description, **kwargs):
        if outcome == "cancelled":
            raise RuntimeError("synthetic undelivered prompt")
        return "deny" if outcome == "denied" else outcome

    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    set_approval_callback(callback)
    target = tmp_path / "AGENTS.md"
    result = json.loads(write_file_tool(str(target), "synthetic content"))

    assert not target.exists()
    assert "Do NOT retry" in result["error"]
    if outcome == "denied":
        assert "was denied by the user. The user has NOT consented" in result["error"]
    else:
        assert "No runtime approval was obtained" in result["error"]
        assert "NOT consented" not in result["error"]
