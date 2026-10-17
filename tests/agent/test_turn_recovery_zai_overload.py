"""Recovery-loop proof for the Z.AI coding-route overload (HTTP 429, code 1305) on the GLM-5.x Flash family.

Drives the real error handler (``handle_api_error``) one failed call at a time, feeding ``retry_count`` and
``max_retries`` back the way ``_run_api_retry_loop`` does. The agent is a stub with a recording credential pool
and fallback chain; the real wait loop runs on a fake clock. An overload must ride the whole schedule (three
short retries, then 30/60/90/120 s) on the same key, reach the reserve only once that schedule is spent, and stop
when cancelled; a quota 1302 must keep failing fast. The classifier and the retry policy must agree on each
code and status shape in the table below, because the verdict decides rotation and the policy decides the wait
and the reserve. Their overload wording differs on purpose (the classifier's broad patterns, the policy's one
phrase); the table uses the phrase both accept.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent import retry_utils
from agent.agent_runtime_helpers import recover_with_credential_pool, try_recover_primary_transport
from agent.error_classifier import ClassifiedError, FailoverReason, classify_api_error
from agent.turn_api_error import handle_api_error
from agent.turn_recovery import route_classified_error
from agent.turn_retry_state import TurnRetryState

_URL = "https://api.z.ai/api/coding/paas/v4"
_MODEL = "glm-5.3-flash"
_PROSE = "The service may be temporarily overloaded, please try again later"
_PLAIN = "please try again later"
_NO_CODE = object()
# Three short retries (the generic wait, pinned to its base by the ``clock`` fixture), then the long tier.
_SCHEDULE = [2.0, 2.0, 2.0, 30.0, 60.0, 90.0, 120.0]


def _nested(code, message=_PLAIN, *, top=None):
    error = {"message": message} if code is _NO_CODE else {"message": message, "code": code}
    return {"error": error, **(top or {})}


class _ZaiError(Exception):
    """SDK-shaped provider error; ``status=None`` is a status-less stream error whose body carries the status."""

    def __init__(self, body, *, status=429, headers=None):
        super().__init__(str((body.get("error") or body).get("message")))
        self.status_code, self.body, self.response = status, body, SimpleNamespace(headers=headers or {})


# (id, body, status, is_overload): what the classifier and the retry policy must both say.
_SHAPES = [
    ("bare 1305", _nested("1305"), 429, True),
    ("1305 with prose", _nested("1305", _PROSE), 429, True),
    ("numeric 1305", _nested(1305), 429, True),
    ("padded 1305", _nested(" 1305 "), 429, True),
    ("flat 1305", {"code": "1305", "message": _PLAIN}, 429, True),
    ("no code, prose", _nested(_NO_CODE, _PROSE), 429, True),
    ("empty code, prose", _nested("", _PROSE), 429, True),
    ("blank code, prose", _nested("  ", _PROSE), 429, True),
    ("status only in body", _nested("1305", top={"code": 429}), None, True),
    ("status only in flat body, prose", {"code": 429, "message": _PROSE}, None, True),
    ("quota 1302", _nested("1302", "Rate limit reached for requests"), 429, False),
    ("quota 1302 naming 1305", _nested("1302", "Rate limit reached for requests (ref 1305)"), 429, False),
    ("quota 1302, overload prose", _nested("1302", "temporarily overloaded by your quota window"), 429, False),
    ("numeric 1302, overload prose", _nested(1302, _PROSE), 429, False),
    ("no code, no overload", _nested(_NO_CODE, "Rate limit reached for requests"), 429, False),
]
_OVERLOADS = [s for s in _SHAPES if s[3]]


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    # The handler imports agent.conversation_loop lazily; its launcher machinery resolves the hermes home,
    # and the suite's home-IO guard forbids the real one.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # No real fallback restart: the recorded activation and the "break" verdict are the assertions.
    monkeypatch.setattr("agent.conversation_loop._arm_fallback_restart", lambda agent, messages, prompt, retry: prompt)
    monkeypatch.setattr("agent.conversation_loop._is_copilot_provider", lambda agent: False)


class _Clock:
    """Stands in for ``time`` inside ``agent.turn_recovery``: sleeping advances it, so the real wait loop is instant."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr("agent.turn_recovery.time", fake)
    # tests/agent/conftest.py pins the jittered wait to 0.0 for loop speed; pin it to its base so the bands are exact.
    monkeypatch.setattr(retry_utils, "jittered_backoff", lambda *a, **kw: kw["base_delay"])
    return fake


class _Pool:
    """One credential; records every rotation. A rotation with no second entry recovers nothing."""

    def __init__(self):
        self.rotations = []

    def entries(self):
        return [SimpleNamespace(runtime_api_key="sk-test")]

    def has_available(self):
        return True

    def mark_exhausted_and_rotate(self, **kwargs):
        self.rotations.append(kwargs)


class _Agent:
    provider = "zai"
    model = _MODEL
    base_url = _URL
    api_key = "sk-test"
    log_prefix = ""
    verbose = False
    verbose_logging = False
    _interrupt_requested = False
    _fallback_activated = False

    def __init__(self, clock):
        self.clock = clock
        self._credential_pool = self.pool = _Pool()
        self._fallback_chain = [{"provider": "codexlb", "model": "gpt-6.1-sol"}]
        self._fallback_index = 0
        self._image_rejecting_models = set()
        self.fallback_reasons = []

    def _has_pending_fallback(self):
        return self._fallback_index < len(self._fallback_chain)

    def _try_activate_fallback(self, reason=None, reset_at=None):
        self.fallback_reasons.append(reason)
        self._fallback_index += 1
        self._fallback_activated = True
        return True

    def _recover_with_credential_pool(self, **kwargs):
        return recover_with_credential_pool(self, **kwargs)

    def _try_recover_primary_transport(self, api_error, *, retry_count, max_retries):
        return try_recover_primary_transport(self, api_error, retry_count=retry_count, max_retries=max_retries)

    def _is_openrouter_url(self):
        return False

    def _summarize_api_error(self, error):
        return str(error)

    def _client_log_context(self):
        return ""

    def clear_interrupt(self, **kwargs):
        self._interrupt_requested = False
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _drive(agent, errors):
    """Feed ``errors`` to the real handler as ``_run_api_retry_loop`` does: while the budget lasts and the
    handler asks for another try. Returns ``[(verdict, fallbacks_so_far, wait_seconds)]``."""
    retry, retry_count, max_retries, trail = TurnRetryState(), 0, 3, []
    for error in errors:
        if retry_count >= max_retries:
            break
        started = agent.clock.now
        verdict = handle_api_error(
            agent, api_error=error, _retry=retry, thinking_spinner=None, messages=[], api_messages=[], api_kwargs={},
            system_message=None, active_system_prompt="", conversation_history=[], approx_tokens=10,
            retry_count=retry_count, max_retries=max_retries, compression_attempts=0, max_compression_attempts=2,
            api_call_count=len(trail) + 1, api_request_id="r", api_start_time=time.time(), effective_task_id=None,
            turn_id="t",
        )
        trail.append((verdict, len(agent.fallback_reasons), agent.clock.now - started))
        retry_count, max_retries = verdict.retry_count, verdict.max_retries
        if verdict.action in ("return", "break"):
            break
    return trail


@pytest.fixture
def agent(clock):
    return _Agent(clock)


@pytest.mark.parametrize("name, body, status, expected", _SHAPES, ids=[s[0] for s in _SHAPES])
def test_classifier_and_retry_policy_agree_on_each_code_and_status_shape(name, body, status, expected):
    error = _ZaiError(body, status=status)
    verdict = classify_api_error(error, provider="zai", model=_MODEL, base_url=_URL)
    policy = retry_utils.is_zai_coding_overload_error(base_url=_URL, model=_MODEL, error=error)

    assert (verdict.reason is FailoverReason.overloaded) == policy == expected, name
    # An overload verdict is what keeps the credential: no rotation, no benching.
    assert verdict.should_rotate_credential is (not expected), name


@pytest.mark.parametrize("name, body, status, expected", _OVERLOADS, ids=[s[0] for s in _OVERLOADS])
def test_overload_rides_the_whole_schedule_on_one_key_then_reaches_the_reserve_once(agent, name, body, status, expected):
    trail = _drive(agent, [_ZaiError(body, status=status) for _ in range(12)])
    *waited, ceiling = trail

    # Seven failed calls each retry after their wait; none touches the reserve or the credential pool.
    assert [v.action for v, _, _ in waited] == ["fallthrough"] * 7
    assert [fallbacks for _, fallbacks, _ in waited] == [0] * 7
    assert [seconds for _, _, seconds in waited] == pytest.approx(_SCHEDULE, abs=0.25)
    assert {v.max_retries for v, _, _ in trail} == {retry_utils.zai_coding_overload_retry_ceiling()} == {8}
    # The eighth failure spends the ceiling: only now does the reserve take over, once, without another wait.
    verdict, fallbacks, seconds = ceiling
    assert (verdict.action, fallbacks, seconds) == ("break", 1, 0)
    assert agent.fallback_reasons == [FailoverReason.overloaded]
    assert agent.pool.rotations == []


def test_quota_1302_keeps_failing_fast(agent):
    trail = _drive(agent, [_ZaiError(_nested("1302", "Rate limit reached for requests")) for _ in range(4)])

    # A quota wall gets no long wait and no raised ceiling: the reserve takes over on the first failure.
    assert [(v.action, v.max_retries, seconds) for v, _, seconds in trail] == [("break", 3, 0)]
    assert agent.fallback_reasons == [FailoverReason.rate_limit]


def test_retry_after_beats_the_schedule_and_is_capped(agent):
    errors = [_ZaiError(_nested("1305"), headers={"Retry-After": value}) for value in ("7", "9999")]
    trail = _drive(agent, errors)

    assert [seconds for _, _, seconds in trail] == pytest.approx([7.0, retry_utils.RETRY_AFTER_CAP_S], abs=0.25)
    assert [v.max_retries for v, _, _ in trail] == [8, 8]
    assert agent.fallback_reasons == []


def test_cancel_during_a_long_wait_stops_the_schedule_before_the_reserve(agent, clock, monkeypatch):
    start = clock.now

    def cancel_in_the_fourth_wait(seconds):
        clock.now += seconds
        if clock.now - start > sum(_SCHEDULE[:3]) + 5:
            agent._interrupt_requested = True

    monkeypatch.setattr(clock, "sleep", cancel_in_the_fourth_wait)
    trail = _drive(agent, [_ZaiError(_nested("1305")) for _ in range(12)])

    verdict, fallbacks, seconds = trail[-1]
    assert len(trail) == 4 and verdict.action == "return" and verdict.result["interrupted"] is True
    assert 0 < seconds < _SCHEDULE[3] and fallbacks == 0
    assert agent.fallback_reasons == [] and agent.pool.rotations == []


def _route(agent, api_error, classified):
    return route_classified_error(
        agent, api_error, classified, TurnRetryState(), error_msg=str(api_error), error_context=None,
        recovered_with_pool=False, base_url=_URL, model=_MODEL, messages=[], api_messages=[], system_message=None,
        active_system_prompt=None, conversation_history=[], retry_count=2, max_retries=3, compression_attempts=0,
        max_compression_attempts=2, api_call_count=1, effective_task_id=None,
    )


def test_upstream_429_still_falls_back_immediately(agent):
    classified = ClassifiedError(reason=FailoverReason.upstream_rate_limit, status_code=429)

    verdict = _route(agent, SimpleNamespace(status_code=429), classified)

    assert agent.fallback_reasons, "upstream-aggregator 429 must attempt the reserve"
    assert verdict.action == "break"
