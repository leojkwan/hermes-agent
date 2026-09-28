"""A resumed chat must never pair its stored model with a provider it did not run on.

Synthetic regression scenario: a voice chat created while the default was the
local llama.cpp model persisted only ``model_config={"model": "example-local-model"}`` plus the bare
``billing_provider="custom"`` bucket, with no provider identity. The default then moved to openai-codex /
gpt-6-sol. ``session.resume`` restored the stored model name and resolved the provider from config, so every
turn sent the llama.cpp model to Codex: HTTP 400 "model is not supported when using Codex with a ChatGPT account".

These tests drive the real ``session.resume`` dispatch (cold, deferred and eager builds) against a real
state.db row of that shape, through the real override decode, deferred-build kwargs, ``_make_agent`` and
``resolve_runtime_provider``. Only the ``AIAgent`` constructor is replaced: its kwargs are the runtime the
first turn's request goes out with.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli.local_runtime import endpoint
from hermes_state import SessionDB
from tui_gateway import server

STORED_MODEL = "example-local-model"
LOCAL_URL = "http://127.0.0.1:12345/v1"
CODEX_URL = "https://chatgpt.com/backend-api/codex"
CLOUD_URL = "https://cloud.invalid/v1"
# configured default provider -> (its default model, the endpoint it resolves to)
DEFAULTS = {
    "openai-codex": ("gpt-6-sol", CODEX_URL),  # synthetic provider switch
    "custom:cloud": ("glm-5.3-flashx", CLOUD_URL),  # custom-class default: no curated catalog to consult
}
MODES = {"cold": {}, "deferred": {"defer_history": True}, "eager": {"eager_build": True}}


class _Built(Exception):
    """Raised by the fake AIAgent once it has recorded the runtime it was handed."""


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    for var in ("HERMES_MODEL", "HERMES_INFERENCE_MODEL", "HERMES_TUI_PROVIDER", "HERMES_INFERENCE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    db = SessionDB(db_path=tmp_path / "state.db")
    built: list[dict] = []

    def fake_agent(**kwargs):
        built.append(kwargs)
        raise _Built

    monkeypatch.setattr("run_agent.AIAgent", fake_agent)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(rp, "resolve_codex_runtime_credentials",
                        lambda: {"base_url": CODEX_URL, "api_key": "tok", "source": "hermes-auth-store",
                                 "last_refresh": 1})
    monkeypatch.setattr(rp, "load_pool", lambda _provider: SimpleNamespace(has_credentials=lambda: False))
    # The managed llama.cpp server the chat originally ran on is still up at its loopback URL.
    local_route = {"base_url": LOCAL_URL, "api_key": "local-test-key"}
    monkeypatch.setattr(endpoint, "_state_endpoint", lambda: local_route)
    monkeypatch.setattr(endpoint, "resolve_llamacpp_endpoint", lambda **_kw: local_route)

    def configure(provider: str) -> None:
        cfg = {"model": {"provider": provider, "default": DEFAULTS[provider][0]},
               "custom_providers": [{"name": "cloud", "base_url": CLOUD_URL, "api_key": "sk-cloud",
                                     "api_mode": "chat_completions"}],
               "local_runtime": {"enabled": True}}
        monkeypatch.setattr(rp, "load_config", lambda: cfg)
        monkeypatch.setattr(rp, "_get_model_config", lambda: cfg["model"])
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
        monkeypatch.setattr(server, "_load_cfg", lambda: cfg)

    def seed(model_config: dict) -> str:
        sid = "synthetic-resume-session"
        db.create_session(sid, source="voice-safari", model=STORED_MODEL, model_config=model_config)
        db.update_session_billing_route(sid, provider="custom", base_url=LOCAL_URL)
        db.append_message(sid, "user", "what's on my plate tonight?")
        db.append_message(sid, "assistant", "Two things.")
        return sid

    live: list[str] = []

    def resume(sid: str, mode: str) -> dict:
        """Resume, let the agent build run to the AIAgent constructor, return the resume response."""
        resp = server.handle_request(
            {"id": "resume", "method": "session.resume", "params": {"session_id": sid, **MODES[mode]}})
        if live_sid := (resp.get("result") or {}).get("session_id"):
            live.append(live_sid)
            assert server._sessions[live_sid]["agent_ready"].wait(10), "agent build never finished"
        assert len(built) == 1, f"expected one agent build, got {len(built)}: {resp}"
        return resp

    yield SimpleNamespace(configure=configure, seed=seed, resume=resume, built=built)
    for live_sid in live:
        server.handle_request({"id": "close", "method": "session.close", "params": {"session_id": live_sid}})
    db.close()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("default_provider", DEFAULTS)
def test_providerless_stored_model_follows_the_configured_default_pair(gateway, default_provider, mode):
    gateway.configure(default_provider)
    default_model, default_url = DEFAULTS[default_provider]
    sid = gateway.seed({"model": STORED_MODEL})

    resp = gateway.resume(sid, mode)

    agent = gateway.built[0]
    assert (agent["model"], agent["base_url"]) == (default_model, default_url), (
        f"resumed turn would send {agent['model']!r} to {agent['base_url']!r}")
    if mode != "eager":  # eager reports info from the built agent, which the fake never returns
        assert resp["result"]["info"]["model"] == default_model


@pytest.mark.parametrize("mode", MODES)
def test_stored_pick_with_its_provider_survives_a_default_change(gateway, mode):
    """A /model switch persists its provider; that complete pair is the chat's own and still wins."""
    gateway.configure("openai-codex")
    sid = gateway.seed({"model": STORED_MODEL, "provider": "llamacpp", "base_url": LOCAL_URL,
                        "api_mode": "chat_completions"})

    gateway.resume(sid, mode)

    agent = gateway.built[0]
    assert (agent["model"], agent["base_url"]) == (STORED_MODEL, LOCAL_URL)
