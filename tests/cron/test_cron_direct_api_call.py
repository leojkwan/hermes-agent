"""Regression guard for #62151 — gateway cron must not wedge on 2nd+ API call.

Gateway-fired cron jobs hung forever on the 2nd+ non-streaming API call when
``interruptible_api_call`` spawned a daemon worker inside nested cron thread
pools. The worker logged client creation but never opened a TCP connection.
The same job succeeded via ``hermes cron tick``. Cron has no interactive
interrupt surface, so the fix routes cron through a synchronous direct call on
the conversation thread instead of the interrupt worker.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.chat_completion_helpers import (
    direct_api_call,
)


def _make_agent(*, platform="cron"):
    agent = MagicMock()
    agent.platform = platform
    agent.api_mode = "chat_completions"
    agent.provider = "openrouter"
    agent._interrupt_requested = False
    agent._touch_activity = MagicMock()
    agent._create_request_openai_client = MagicMock()
    agent._close_request_openai_client = MagicMock()
    return agent




def test_direct_api_call_keeps_activity_alive_during_slow_wait(monkeypatch):
    """Mid-wait activity heartbeats must tick while the inline request blocks.

    Subagents use direct_api_call (non-streaming). Without mid-call
    ``_touch_activity`` ticks, the async stall monitor treats a slow-but-
    healthy local model wait as frozen progress and interrupts around 450s
    (``Operation interrupted: waiting for model response``).
    """
    import threading
    import time

    from agent import chat_completion_helpers as helpers

    monkeypatch.setattr(helpers, "_DIRECT_API_ACTIVITY_HEARTBEAT_SECONDS", 0.05)

    agent = _make_agent(platform="subagent")
    started = threading.Event()
    release = threading.Event()
    fake_client = MagicMock()

    def _slow_create(**_kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return SimpleNamespace(id="slow")

    fake_client.chat.completions.create.side_effect = _slow_create
    agent._create_request_openai_client.return_value = fake_client

    result_box = {}

    def _runner():
        result_box["response"] = direct_api_call(
            agent, {"model": "m", "messages": []}
        )

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    assert started.wait(timeout=2.0)

    # Allow several heartbeat intervals while the request is still blocked.
    deadline = time.time() + 1.0
    while time.time() < deadline and agent._touch_activity.call_count < 3:
        time.sleep(0.05)

    touches_while_blocked = agent._touch_activity.call_count
    release.set()
    worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert result_box["response"].id == "slow"
    assert touches_while_blocked >= 3, (
        f"expected mid-wait activity heartbeats, got {touches_while_blocked}"
    )


def test_direct_api_call_heartbeat_stops_on_exception(monkeypatch):
    """The activity heartbeat thread must be joined on error paths so no
    stray _touch_activity fires after the call has failed.
    """
    import threading
    import time

    from agent import chat_completion_helpers as helpers

    monkeypatch.setattr(helpers, "_DIRECT_API_ACTIVITY_HEARTBEAT_SECONDS", 0.05)

    agent = _make_agent(platform="subagent")
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = RuntimeError("provider down")
    agent._create_request_openai_client.return_value = fake_client

    raised = threading.Event()

    def _runner():
        try:
            direct_api_call(agent, {"model": "m", "messages": []})
        except RuntimeError:
            raised.set()

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout=3.0)

    assert raised.is_set(), "expected RuntimeError from direct_api_call"
    # Give any stray heartbeat a chance to fire after the call returned.
    time.sleep(0.2)
    touches_before = agent._touch_activity.call_count
    time.sleep(0.2)
    touches_after = agent._touch_activity.call_count
    assert touches_after == touches_before, (
        "heartbeat thread still firing after direct_api_call raised"
    )


def _configured_cron_request(
    monkeypatch,
    tmp_path,
    tier,
    *,
    provider="openai-codex",
    base_url="https://chatgpt.com/backend-api/codex",
    runtime_tier=None,
    configured_tier="Fast",
):
    """Real cron/agent/request builders and SDK serialization; only HTTP and catalog lookup fake."""
    import json

    import httpx
    from openai import OpenAI

    from cron.scheduler import _construct_cron_agent, _CronAgentSetup
    from hermes_cli.config import load_config_readonly
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"agent:\n  service_tier: {configured_tier}\n"
        "custom_providers:\n  - name: cron-proxy\n    base_url: https://proxy.example/v1\n"
        "    extra_body:\n      service_tier: priority\n      custom_trace: preserved\n",
        encoding="utf-8",
    )
    config = load_config_readonly()
    emitted = []

    def receive(request):
        emitted.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "cron-tier-test",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-6-astra",
                "output": [],
            },
        )

    client = OpenAI(
        api_key="test-key",
        base_url=base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(receive)),
    )
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", dict)
    # Unknown entitlement keeps the existing negotiation policy without any account lookup.
    monkeypatch.setattr(
        "agent.codex_service_tiers._tiers_for_model", lambda *args: None
    )
    overrides = {"extra_body": {"cron_trace": "preserved"}}
    if runtime_tier:
        overrides["service_tier"] = runtime_tier
    original_overrides = {**overrides, "extra_body": dict(overrides["extra_body"])}
    setup = _CronAgentSetup(
        model="gpt-6-astra",
        runtime={
            "api_key": "test-key",
            "provider": provider,
            "base_url": base_url,
            "api_mode": "codex_responses",
            "request_overrides": overrides,
        },
        max_iterations=1,
    )
    agent = _construct_cron_agent(
        AIAgent,
        {"service_tier": tier},
        config,
        setup,
        workdir=None,
        session_id="cron-tier-test",
        session_db=None,
    )

    def send():
        agent.client.responses.create(
            **agent._build_api_kwargs([{"role": "user", "content": "hello"}], [])
        )
        assert emitted[-1]["cron_trace"] == "preserved"
        if agent.provider == "custom":
            assert emitted[-1]["custom_trace"] == "preserved"
        assert overrides == original_overrides
        return emitted[-1]

    return agent, send, client


@pytest.mark.parametrize(
    "tier,route,runtime_tier,expected",
    [
        ("Fast", "codex", "default", "priority"),
        ("priority", "codex", None, "priority"),
        ("ultra", "codex", None, "ultrafast"),
        ("default", "codex", None, None),
        ("normal", "codex", "priority", None),
        (None, "codex", None, None),
        (None, "codex", "default", "default"),
        ("Fast", "proxy", None, None),
        ("normal", "proxy", None, None),
    ],
)
def test_cron_job_static_tier_reaches_request(
    monkeypatch, tmp_path, tier, route, runtime_tier, expected
):
    route_kwargs = (
        {}
        if route == "codex"
        else {"provider": "custom", "base_url": "https://proxy.example/v1"}
    )
    _agent, send, client = _configured_cron_request(
        monkeypatch, tmp_path, tier, runtime_tier=runtime_tier, **route_kwargs
    )
    try:
        request = send()
        assert request.get("service_tier") == expected
        assert request["cron_trace"] == "preserved"
        if route == "proxy":
            assert request["custom_trace"] == "preserved"
    finally:
        client.close()


@pytest.mark.parametrize("tier", ["auto", "cold"])
def test_cron_job_bounded_tier_keeps_request_window(monkeypatch, tmp_path, tier):
    from agent import fast_mode

    clock = [1000.0]
    monkeypatch.setattr(fast_mode.time, "monotonic", lambda: clock[0])
    agent, send, client = _configured_cron_request(monkeypatch, tmp_path, tier)
    try:
        assert agent.service_tier == tier
        assert "service_tier" not in agent.request_overrides
        fast_mode.begin_turn(agent, [])
        assert send()["service_tier"] == "priority"
        clock[0] += agent.fast_auto_seconds + 1
        assert "service_tier" not in send()
        fast_mode.begin_turn(agent, [{"role": "user", "content": "earlier"}])
        assert send().get("service_tier") == ("priority" if tier == "auto" else None)
    finally:
        client.close()


def test_cron_explicit_fast_ignores_normal_interactive(monkeypatch, tmp_path):
    _agent, send, client = _configured_cron_request(
        monkeypatch, tmp_path, "Fast", configured_tier="normal"
    )
    try:
        assert send()["service_tier"] == "priority"
    finally:
        client.close()


@pytest.mark.parametrize("tier", ["normal", "Fast", "auto", "cold", None])
@pytest.mark.parametrize("primary", ["codex", "proxy"])
def test_cron_job_tier_survives_fallback_and_primary_restore(
    monkeypatch, tmp_path, tier, primary
):
    """The actual fallback binder and gated restore feed real serialized requests."""
    from agent import fast_mode

    clock = [1000.0]
    monkeypatch.setattr(fast_mode.time, "monotonic", lambda: clock[0])
    codex = ("openai-codex", "https://chatgpt.com/backend-api/codex")
    proxy = ("custom", "https://proxy.example/v1")
    provider, base_url = codex if primary == "codex" else proxy
    agent, send, client = _configured_cron_request(
        monkeypatch, tmp_path, tier, provider=provider, base_url=base_url
    )
    fallback_provider, fallback_url = proxy if primary == "codex" else codex
    fallback_client = client.with_options(base_url=fallback_url)
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *args, **kwargs: (fallback_client, "gpt-6-astra"),
    )
    agent._fallback_chain = [
        {
            "provider": fallback_provider,
            "model": "gpt-6-astra",
            "base_url": fallback_url,
            "api_key": "test-key",
            "api_mode": "codex_responses",
        }
    ]

    def expected(route, *, expired=False):
        if tier is None:
            return "priority" if route == "custom" else None
        if tier == "normal" or route == "custom" or expired:
            return None
        return "priority"

    try:
        fast_mode.begin_turn(agent, [])
        assert send().get("service_tier") == expected(provider)
        assert agent._try_activate_fallback() is True
        assert agent.provider == fallback_provider
        assert send().get("service_tier") == expected(fallback_provider)
        if tier in ("auto", "cold"):
            clock[0] += agent.fast_auto_seconds + 1
            assert send().get("service_tier") == expected(
                fallback_provider, expired=True
            )
        assert agent._restore_primary_runtime() is True
        assert agent.provider == provider
        assert send().get("service_tier") == expected(
            provider, expired=tier in ("auto", "cold")
        )
        fast_mode.begin_turn(agent, [{"role": "user", "content": "earlier"}])
        assert send().get("service_tier") == expected(provider, expired=tier == "cold")
    finally:
        fallback_client.close()
        client.close()


def test_cron_fast_rechecks_fallback_model(monkeypatch, tmp_path):
    agent, send, client = _configured_cron_request(monkeypatch, tmp_path, "Fast")
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *args, **kwargs: (client, None),
    )
    agent._fallback_chain = [
        {
            "provider": "openai-codex",
            "model": "unsupported-model",
            "base_url": str(client.base_url),
            "api_key": "test-key",
            "api_mode": "codex_responses",
        }
    ]
    try:
        assert send()["service_tier"] == "priority"
        assert agent._try_activate_fallback() is True
        assert agent.model == "unsupported-model"
        assert "service_tier" not in send()
        assert agent._restore_primary_runtime() is True
        assert send()["service_tier"] == "priority"
    finally:
        client.close()


@pytest.mark.parametrize(
    "offered,expected", [(frozenset(), None), (frozenset({"priority"}), "priority")]
)
def test_cron_policy_preserves_current_credential_negotiation(
    monkeypatch, tmp_path, offered, expected
):
    agent, send, client = _configured_cron_request(monkeypatch, tmp_path, "ultra")
    fallback_client = client.with_options(api_key="fallback-key")
    monkeypatch.setattr(
        "agent.codex_service_tiers._tiers_for_model",
        lambda key, base, model: (
            frozenset({"priority", "ultrafast"}) if key == "test-key" else offered
        ),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *args, **kwargs: (fallback_client, None),
    )
    agent._fallback_chain = [
        {
            "provider": "openai-codex",
            "model": "gpt-6.1-sol",
            "base_url": str(client.base_url),
            "api_key": "fallback-key",
            "api_mode": "codex_responses",
        }
    ]
    try:
        assert send()["service_tier"] == "ultrafast"
        assert agent._try_activate_fallback() is True
        assert agent.api_key == "fallback-key"
        assert send().get("service_tier") == expected
        assert agent._restore_primary_runtime() is True
        assert send()["service_tier"] == "ultrafast"
    finally:
        fallback_client.close()
        client.close()
