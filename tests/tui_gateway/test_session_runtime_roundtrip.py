"""An intentional TUI model switch must replace the route every resume reader sees."""

import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from hermes_cli.cli_model_switch_mixin import stored_session_route
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def saved_session(tmp_path):
    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        db.create_session("parent", source="telegram", model="previous-model")
        db.create_session(
            "switched",
            source="telegram",
            parent_session_id="parent",
            model="previous-model",
            system_prompt="Synthetic cached prompt",
            model_config={
                "_branched_from": "parent",
                "session_note": {"purpose": "synthetic round-trip"},
                "provider": "anthropic",
                "base_url": "https://previous.invalid/v1",
                "api_mode": "anthropic_messages",
                "reasoning_config": {"effort": "low"},
                "service_tier": "priority",
                "gateway_runtime": {
                    "provider": "anthropic",
                    "base_url": "https://previous.invalid/v1",
                    "api_mode": "anthropic_messages",
                    "fallback_active": True,
                },
            },
        )
        db.append_message("switched", "user", "Synthetic question")
        db.append_message("switched", "assistant", "Synthetic answer")
        yield db


def test_model_switch_roundtrip_restores_current_route_and_effort(saved_session):
    db = saved_session
    before = db.get_session("switched")
    history = db.get_messages_as_conversation("switched")
    agent = SimpleNamespace(
        model="current-model",
        provider="openai",
        requested_provider="anthropic",
        base_url="https://current.invalid/v1",
        api_mode="responses",
        reasoning_config={"enabled": True, "effort": "high"},
        service_tier=None,
        api_key="synthetic-key-must-not-persist",
        _session_db=db,
    )
    server._persist_live_session_runtime(
        {"agent": agent, "session_key": "switched", "create_service_tier_override": ""}
    )

    with closing(SessionDB(db_path=db.db_path)) as reopened:
        row = reopened.get_session("switched")
        route = {
            "provider": agent.provider,
            "base_url": agent.base_url,
            "api_mode": agent.api_mode,
        }
        canonical = SessionDB.session_gateway_runtime(row)
        assert {key: canonical.get(key) for key in route} == route
        assert stored_session_route(
            row, current_model=before["model"], current_provider=agent.requested_provider
        ) == (agent.model, agent.provider, agent.base_url, agent.api_mode, True)
        overrides = server._stored_session_runtime_overrides(row)
        assert overrides["model_override"] == {"model": agent.model, **route}
        assert overrides["provider_override"] == agent.provider
        assert overrides["reasoning_config_override"] == agent.reasoning_config
        assert overrides["service_tier_override"] == ""

        config = json.loads(row["model_config"])
        assert {key: config.get(key) for key in route} == route
        assert config["reasoning_config"] == agent.reasoning_config
        assert config["service_tier"] == "normal"
        assert config["_branched_from"] == "parent"
        assert config["session_note"] == json.loads(before["model_config"])["session_note"]
        assert config["gateway_runtime"]["fallback_active"] is True
        assert agent.api_key not in row["model_config"]
        assert {k: v for k, v in row.items() if k not in {"model", "model_config"}} == {
            k: v for k, v in before.items() if k not in {"model", "model_config"}
        }
        assert reopened.get_messages_as_conversation("switched") == history


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_model_switch_clears_stale_route_keys_in_both_shapes(saved_session, empty):
    db = saved_session
    agent = SimpleNamespace(
        model="current-model",
        provider="openai",
        base_url=empty,
        api_mode=empty,
        reasoning_config={},
        service_tier=None,
        _session_db=db,
    )
    # First switch retains a provider but deliberately drops the old endpoint and wire.
    # A second switch also clears the provider; none of the nested keys may return.
    for provider in ("openai", empty):
        agent.provider = provider
        server._persist_live_session_runtime({"agent": agent, "session_key": "switched"})
        with closing(SessionDB(db_path=db.db_path)) as reopened:
            row = reopened.get_session("switched")
            config = json.loads(row["model_config"])
            expected = {"provider": provider} if provider == "openai" else {}
            for shape in (config, config.get("gateway_runtime", {})):
                assert "base_url" not in shape
                assert "api_mode" not in shape
                if not expected:
                    assert "provider" not in shape
            canonical = SessionDB.session_gateway_runtime(row)
            assert {
                key: canonical[key]
                for key in ("provider", "base_url", "api_mode")
                if key in canonical
            } == expected
            assert stored_session_route(
                row, current_model="previous-model", current_provider="anthropic"
            ) == (agent.model, expected.get("provider"), None, None, bool(expected))
            overrides = server._stored_session_runtime_overrides(row)
            assert overrides["model_override"] == {
                "model": agent.model, "provider": expected.get("provider"), "base_url": None, "api_mode": None
            }
            assert overrides["reasoning_config_override"] == {}
            assert "service_tier_override" not in overrides
            assert config["gateway_runtime"]["fallback_active"] is True
