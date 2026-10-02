"""Offline SDK controls for the canonical credential rung, not live-provider proof."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI


@pytest.fixture
def aux(monkeypatch):
    source = Path(os.environ.get("AR01_SOURCE", str(Path(__file__).parents[2] / "agent" / "auxiliary_client.py")))
    name = "ar01_frozen_auxiliary_client"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_auth_refresh_provider_for_route", lambda *a, **k: "zai")
    monkeypatch.setattr(module, "_effective_provider_for_client", lambda *a, **k: "zai")
    monkeypatch.setattr(module, "_refresh_provider_credentials", lambda *a, **k: False)
    monkeypatch.setattr(module, "_evict_cached_clients", lambda *a, **k: None)
    return module


def run_control(aux, monkeypatch, mode, pool_state, *, eligible=True, status=429):
    requests = []

    def handler(request):
        requests.append(request)
        if pool_state == "usable" and len(requests) == 2:
            return httpx.Response(200, json={
                "id": "offline-ok", "object": "chat.completion", "created": 0,
                "model": "offline-control", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop"}],
            })
        return httpx.Response(status, json={"error": {"message": "offline rate-limit control"}})

    def load_pool(_provider):
        if pool_state == "missing":
            return None
        if pool_state == "unreadable":
            raise OSError("offline unreadable-pool control")
        def has_credentials():
            if pool_state == "invalid":
                raise ValueError("offline invalid-pool control")
            return pool_state == "usable"
        return SimpleNamespace(has_credentials=has_credentials,
            mark_exhausted_and_rotate=lambda **k: None, try_refresh_current=lambda: None)

    monkeypatch.setattr(aux, "load_pool", load_pool)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **k: "zai" if eligible else None)
    transport = httpx.MockTransport(handler)
    kwargs = {"model": "offline-control", "messages": [{"role": "user", "content": "offline"}]}

    def route_for(client):
        return SimpleNamespace(client=client, task="goal_judge", tag="",
            resolved_provider="zai", resolved_model="offline-control", final_model="offline-control",
            base_info={}, main_runtime={})

    def sync():
        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1",
            max_retries=0, http_client=httpx.Client(transport=transport))
        try:
            with pytest.raises(Exception) as failure:
                client.chat.completions.create(**kwargs)
            original_error = failure.value
            ladder = aux._ladder_credential_rungs(original_error, route_for(client), kwargs, False)
            def perform(step):
                assert step.kind == "call", "unexpected refresh, rotation, or fallback"
                return step.args[0].chat.completions.create(**step.args[1])
            response, error = aux._drive_ladder(ladder, perform)
            return requests, response, error, original_error
        finally:
            client.close()

    async def async_():
        client = AsyncOpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1",
            max_retries=0, http_client=httpx.AsyncClient(transport=transport))
        try:
            with pytest.raises(Exception) as failure:
                await client.chat.completions.create(**kwargs)
            original_error = failure.value
            ladder = aux._ladder_credential_rungs(original_error, route_for(client), kwargs, False)
            async def perform(step):
                assert step.kind == "call", "unexpected refresh, rotation, or fallback"
                return await step.args[0].chat.completions.create(**step.args[1])
            response, error = await aux._drive_ladder_async(ladder, perform)
            return requests, response, error, original_error
        finally:
            await client.close()

    return sync() if mode == "sync" else asyncio.run(async_())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("pool_state", ["missing", "empty", "unreadable", "invalid"])
def test_no_recovery_pool_does_not_repeat_failed_key(aux, monkeypatch, mode, pool_state):
    requests, response, error, original = run_control(aux, monkeypatch, mode, pool_state)
    assert len(requests) == 1
    assert response is None
    assert error is original


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_usable_pool_preserves_existing_retry(aux, monkeypatch, mode):
    requests, response, error, _ = run_control(aux, monkeypatch, mode, "usable")
    assert len(requests) == 2
    assert response.choices[0].message.content == "ok"
    assert error is None


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_ineligible_endpoint_does_not_retry(aux, monkeypatch, mode):
    requests, response, error, original = run_control(aux, monkeypatch, mode, "usable", eligible=False)
    assert len(requests) == 1
    assert response is None
    assert error is original


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_noncredential_failure_remains_untouched(aux, monkeypatch, mode):
    requests, response, error, original = run_control(aux, monkeypatch, mode, "missing", status=500)
    assert len(requests) == 1
    assert response is None
    assert error is original
