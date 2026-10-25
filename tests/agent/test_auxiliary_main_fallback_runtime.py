"""The main-model safety net must retain the live route and failed-backend identity."""

from contextlib import nullcontext

import httpx
import pytest
from openai import OpenAI

import agent.auxiliary_client as aux
import hermes_yaml as yaml


@pytest.fixture
def selected_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {
            "provider": "custom", "default": "disk-model",
            "base_url": "http://127.0.0.1:17778/v1", "api_key": "synthetic-disk-key",
        },
    }))
    aux.clear_runtime_main()
    yield {
        "provider": "custom", "requested_provider": "selected-lane",
        "model": "live-model", "base_url": "http://127.0.0.1:17777/v1",
        "api_key": "synthetic-live-key", "api_mode": "chat_completions",
    }
    aux.clear_runtime_main()


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_responses"])
@pytest.mark.parametrize("explicit", [False, True])
def test_main_fallback_keeps_selected_runtime(selected_runtime, api_mode, explicit, monkeypatch):
    runtime = {**selected_runtime, "api_mode": api_mode}
    binding = nullcontext() if explicit else aux.scoped_runtime_main(runtime)
    kwargs = {"main_runtime": runtime} if explicit else {}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0,
            "model": runtime["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "routed"}}],
        })

    def build_client(**options):
        return OpenAI(**options, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(aux, "_create_openai_client", build_client)
    with binding:
        client, model, label = aux._try_main_agent_model_fallback(
            "unavailable-lane", task="compression", failed_model="unavailable-model",
            failed_base_url="http://127.0.0.1:17776/v1", **kwargs,
        )
    assert client is not None
    raw = getattr(client, "_real_client", client)
    try:
        assert str(raw.base_url).rstrip("/") == runtime["base_url"]
        assert raw.api_key == runtime["api_key"]
        assert model == runtime["model"]
        assert label == "main-agent(custom)"
        assert isinstance(client, aux.CodexAuxiliaryClient) == (api_mode == "codex_responses")
        if api_mode == "chat_completions":
            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "synthetic"}],
            )
            assert response.choices[0].message.content == "routed"
            assert len(requests) == 1
            assert str(requests[0].url) == runtime["base_url"] + "/chat/completions"
            assert requests[0].headers["authorization"] == "Bearer " + runtime["api_key"]
    finally:
        raw.close()


@pytest.mark.parametrize("same_model", [True, False])
def test_main_fallback_skips_failed_deployment_across_aliases(selected_runtime, same_model):
    with aux.scoped_runtime_main(selected_runtime):
        client, model, label = aux._try_main_agent_model_fallback(
            "selected-lane", task="compression",
            failed_model=selected_runtime["model"] if same_model else "other-model",
            failed_base_url=selected_runtime["base_url"],
        )
    if same_model:
        assert (client, model, label) == (None, None, "")
    else:
        assert client is not None
        try:
            assert str(client.base_url).rstrip("/") == selected_runtime["base_url"]
            assert model == selected_runtime["model"]
        finally:
            client.close()
