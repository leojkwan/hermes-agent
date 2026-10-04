"""Tests for ``agent/turn_api_call.py::handle_api_interrupt`` — the plain (non-redirect)
interrupt that lands mid provider call and records the streamed partial as the interrupted
assistant row."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent.agent_runtime_helpers import _INTERRUPTED_PLACEHOLDER
from agent.repetition_guard import REPETITION_LOOP_INTERRUPTED
from agent.turn_api_call import handle_api_interrupt
from agent.turn_retry_state import TurnRetryState
from run_agent import AIAgent


@pytest.mark.parametrize("provider_raises,observer_raises", [(False, False), (True, False), (False, True)])
def test_logical_backend_progress_preserves_first_output_and_failure(monkeypatch, provider_raises, observer_raises):
    from agent import turn_api_call
    from agent.turn_facade import native_turn_context
    events, spinner_calls, requests = [], [], []
    clock_ns = [0]
    monkeypatch.setattr(turn_api_call.time, "monotonic_ns", lambda: clock_ns[0])
    def observer(event, payload):
        events.append(payload)
        if observer_raises:
            raise RuntimeError("observer failed")
    def provider(request, on_first_delta):
        requests.append(request)
        clock_ns[0] = 5_000_000
        on_first_delta()  # Could be reasoning or tool generation, not visible answer text.
        clock_ns[0] = 10_000_000
        if provider_raises:
            raise InterruptedError("cancelled")
        return "provider response"
    agent = SimpleNamespace(
        api_mode="chat_completions", session_id="s", provider="fixture", model="fixture",
        platform="tui", base_url="https://fixture.invalid", thinking_callback=spinner_calls.append,
        _interruptible_streaming_api_call=provider, _has_pending_redirect=lambda: False,
        event_callback=observer)
    monkeypatch.setattr(turn_api_call, "_should_stream", lambda agent: True)
    monkeypatch.setattr("hermes_cli.middleware.run_llm_execution_middleware",
                        lambda request, invoke, **kwargs: invoke(request))
    request = {"messages": [{"role": "system", "content": "byte-stable system"},
                             {"role": "user", "content": "ask"}]}
    token = native_turn_context.set(("native-A", 0))
    try:
        kwargs = dict(api_kwargs=request, _original_api_kwargs=request, _llm_middleware_trace=[],
            _moa_prepared_request=None, _retry=TurnRetryState(), thinking_spinner=None, retry_count=2,
            api_call_count=3, api_request_id="logical-request", effective_task_id="task", turn_id="native-A",
            interrupted=False)
        if provider_raises:
            with pytest.raises(InterruptedError, match="cancelled"):
                turn_api_call.perform_api_call(agent, **kwargs)
        else:
            assert turn_api_call.perform_api_call(agent, **kwargs).response == "provider response"
        assert [(payload["stage"], payload["state"]) for payload in events] == [
            ("backend_call", "start"), ("output_observed", "observed"), ("backend_call", "end")]
        assert all(payload["turn_id"] == "native-A" for payload in events)
        assert events[-1]["outcome"] == ("interrupted" if provider_raises else "returned")
        assert events[-1]["duration_ms"] == 10
        assert events[1]["elapsed_ms"] == 5
        assert "duration_ms" not in events[1]
        assert spinner_calls == [""]
        assert requests == [request]
        assert request["messages"] == [{"role": "system", "content": "byte-stable system"},
                                        {"role": "user", "content": "ask"}]
    finally:
        native_turn_context.reset(token)


def _bare_agent(streamed: str) -> AIAgent:
    agent = object.__new__(AIAgent)
    agent._pending_redirect = None
    agent._pending_redirect_lock = threading.Lock()
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._current_streamed_assistant_text = streamed
    agent._strip_think_blocks = lambda content: content
    agent.quiet_mode = True
    agent.log_prefix = ""
    agent.thinking_callback = None
    agent._print_fn = lambda *args, **kwargs: None
    agent._persist_session = lambda *args, **kwargs: None
    return agent


def _interrupt(streamed: str):
    messages = [{"role": "user", "content": "start"}]
    verdict = handle_api_interrupt(
        _bare_agent(streamed), _retry=TurnRetryState(), thinking_spinner=None, messages=messages,
        conversation_history=[], api_start_time=time.time(), interrupted=False, final_response=None,
    )
    return messages, verdict


def test_repetition_dominated_partial_is_not_kept_as_the_interrupted_row():
    """A looped partial replayed as the interrupted assistant row re-seeds the loop on the next
    turn (#112764): the row keeps the neutral placeholder and the user is told what happened."""
    looped = "I. " * 1941

    messages, verdict = _interrupt(looped)

    # Same hidden shape as the redirect placeholder: no visible bubble in transcript replays.
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == ""
    assert messages[-1]["display_kind"] == "hidden"
    assert messages[-1]["api_content"] == _INTERRUPTED_PLACEHOLDER
    assert verdict.final_response == REPETITION_LOOP_INTERRUPTED
    assert "I. I. I." not in verdict.final_response


def test_distinct_batch_rows_are_not_mistaken_for_a_loop():
    """Legitimately repetitive output (distinct INSERT rows sharing a long prefix) trips the
    window scan but is not a runaway loop: the partial must stay the interrupted row and must
    not be relabelled as a degenerate reply."""
    rows = "\n".join(
        f"INSERT INTO users (id, name, email, created_at) VALUES ({i}, 'user{i}', 'user{i}@example.com', NOW());"
        for i in range(12)
    )
    messages, verdict = _interrupt(rows)

    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", rows)
    assert verdict.final_response == rows


def test_ordinary_partial_is_kept_as_the_interrupted_row():
    messages, verdict = _interrupt("Visible draft.")

    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", "Visible draft.")
    assert messages[-1]["display_metadata"] == {"interrupted": True}
    assert verdict.final_response == "Visible draft."
