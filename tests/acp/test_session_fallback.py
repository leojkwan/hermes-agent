"""ACP config reaches the real agent's fallback/retry path; all providers are fakes."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import hermes_yaml as yaml

from acp_adapter.session import SessionManager
from run_agent import AIAgent


class _OfflineAgent(AIAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, skip_context_files=True, skip_memory=True,
                         skip_background_review=True)


class _RateLimitError(Exception):
    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - rate limit exceeded")
        self.response = SimpleNamespace(headers={})
        self.body = {"error": {"message": "rate limit exceeded"}}


def test_acp_configured_fallback_answers_after_zai_429(tmp_path, monkeypatch):
    """Removing ACP's fallback_model argument strands the turn on the 429 primary."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "zai", "default": "glm-5.3-flashx"},
        "fallback_providers": [{"provider": "openai-codex", "model": "gpt-6.1-sol"}],
        # The shared gateway helper appends legacy routes and deduplicates them.
        "fallback_model": [
            {"provider": "openai-codex", "model": "gpt-6.1-sol"},
            {"provider": "zai", "model": "backup-model"},
        ],
        "agent": {"reasoning_overrides": {"gpt-6.1-sol": "xhigh"}},
    }), encoding="utf-8")

    monkeypatch.setattr("run_agent.AIAgent", _OfflineAgent)
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", MagicMock())
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **_kwargs: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda **_kwargs: {})
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_kwargs: {
        "provider": "zai", "api_mode": "chat_completions",
        "base_url": "https://primary.example.invalid/v1", "api_key": "fake-primary-key",
    })
    monkeypatch.setattr("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build", lambda **_kwargs: None)
    # Real credentials are unavailable in the isolated home. Replace only that
    # preflight and client acquisition, keeping fallback selection and retry real.
    monkeypatch.setattr("agent.chat_completion_helpers._fallback_entry_unavailable_without_network",
                        lambda *_args, **_kwargs: None)
    fallback_client = MagicMock()
    fallback_client.base_url = "https://fallback.example.invalid/v1"
    fallback_client.api_key = "fake-fallback-key"
    fallback_client._custom_headers = fallback_client.default_headers = None
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client",
                        lambda *_args, **_kwargs: (fallback_client, "gpt-6.1-sol"))
    monkeypatch.setattr("agent.agent_runtime_helpers.time.sleep", lambda *_args: None)

    manager = SessionManager()
    monkeypatch.setattr(manager, "_get_db", lambda: None)
    agent = manager._make_agent(session_id="acp-fallback-test", cwd=str(tmp_path))
    agent._api_max_retries = 1
    monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *_args: None)
    routes = []

    def fake_api_call(_api_kwargs):
        route = (agent.provider, agent.model)
        routes.append(route)
        if route == ("zai", "glm-5.3-flashx"):
            raise _RateLimitError()
        assert route == ("openai-codex", "gpt-6.1-sol")
        return SimpleNamespace(
            id="offline-response", status="completed", model=agent.model, usage=None,
            output=[SimpleNamespace(
                id="offline-message", type="message", role="assistant", status="completed",
                content=[SimpleNamespace(type="output_text", text="Answered by the configured fallback",
                                         annotations=[])],
            )],
        )

    monkeypatch.setattr(agent, "_interruptible_api_call", fake_api_call)
    result = agent.run_conversation("Answer this offline canary")

    assert result["final_response"] == "Answered by the configured fallback"
    assert result["completed"] is True
    assert routes == [("zai", "glm-5.3-flashx"), ("openai-codex", "gpt-6.1-sol")]
    assert agent._fallback_chain == [
        {"provider": "openai-codex", "model": "gpt-6.1-sol"},
        {"provider": "zai", "model": "backup-model"},
    ]
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
