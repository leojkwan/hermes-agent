"""Recovery-loop proof: a detected Z.AI coding overload must not eagerly fall back.

Drives the real ``route_classified_error`` ordered recovery path with a stub
agent: the Z.AI 1305 case rides the raised retry ceiling (no eager fallback),
while an upstream-aggregator 429 still falls back immediately.
"""

from types import SimpleNamespace

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason, classify_api_error
from agent.turn_recovery import route_classified_error, zai_coding_overload_retry_ceiling
from agent.turn_retry_state import TurnRetryState

_ZAI_CODING_URL = "https://api.z.ai/api/coding/paas/v4"


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    # route_classified_error imports agent.conversation_loop, whose launcher
    # machinery resolves the hermes home; the suite's home-IO guard forbids
    # touching the real one, so point HERMES_HOME at a scratch dir.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _stub_conversation_loop(monkeypatch):
    # The recovery function imports agent.conversation_loop lazily; that module's
    # import chain reads update-custody state under the real home, which the
    # suite's home-IO guard refuses. This test only needs the two symbols the
    # eager-fallback path touches, so inject a stand-in module.
    import sys
    import types

    stub = types.ModuleType("agent.conversation_loop")

    def _arm_fallback_restart(agent, api_messages, active_system_prompt, _retry):
        return active_system_prompt

    def _ra():
        class _RA:
            @staticmethod
            def _pool_may_recover_from_rate_limit(pool):
                return False

        return _RA()

    stub._arm_fallback_restart = _arm_fallback_restart
    stub._ra = _ra
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", stub)


class _StubAgent:
    provider = "zai"

    def __init__(self):
        self._fallback_chain = [{"provider": "codexlb", "model": "gpt-6.1-sol"}]
        self._fallback_index = 0
        self._fallback_calls = []
        self._diagnostics = []

    def _try_activate_fallback(self, reason=None, reset_at=None):
        self._fallback_calls.append(reason)
        return True  # arm the restart; the call record and the break verdict are the assertions

    def _buffer_diagnostic_status(self, message):
        self._diagnostics.append(message)


def _zai_overload_error():
    return SimpleNamespace(
        status_code=429,
        body={"error": {"code": "1305", "message": "The service may be temporarily overloaded, please try again later"}},
    )


def _route(agent, api_error, classified, *, retry_count=2, max_retries=3):
    return route_classified_error(
        agent, api_error, classified, TurnRetryState(), error_msg=str(api_error),
        error_context=None, recovered_with_pool=False, base_url=_ZAI_CODING_URL,
        model="glm-5.3-flash", messages=[], api_messages=[], system_message=None,
        active_system_prompt=None, conversation_history=[], retry_count=retry_count,
        max_retries=max_retries, compression_attempts=0, max_compression_attempts=2,
        api_call_count=1, effective_task_id=None,
    )


def test_zai_coding_overload_does_not_eagerly_fall_back_and_raises_ceiling():
    agent = _StubAgent()
    err = _zai_overload_error()
    classified = classify_api_error(err, provider="zai", model="glm-5.3-flash", base_url=_ZAI_CODING_URL)
    assert classified.reason is FailoverReason.overloaded

    verdict = _route(agent, err, classified, retry_count=2, max_retries=3)

    # Past the transport-failure threshold (retry_count >= 2) an overload used
    # to switch to the reserve; the guard must keep it on the retry path and
    # raise the ceiling so the long backoff schedule can run.
    assert agent._fallback_calls == []
    assert verdict.action == "fallthrough"
    assert verdict.max_retries >= zai_coding_overload_retry_ceiling()


def test_upstream_429_still_falls_back_immediately():
    agent = _StubAgent()
    classified = ClassifiedError(reason=FailoverReason.upstream_rate_limit, status_code=429)

    verdict = _route(agent, SimpleNamespace(status_code=429), classified, retry_count=2, max_retries=3)

    assert agent._fallback_calls, "upstream-aggregator 429 must attempt the reserve"
    assert verdict.action == "break"
