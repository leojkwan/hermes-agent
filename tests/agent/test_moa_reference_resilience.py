"""MoA degrades gracefully when reference backends are unhealthy.

Context: a fan-out ran 2/2 references against one sick backend; both returned an
HTTP 500 envelope wrapping a Vertex ``400 INVALID_ARGUMENT``, and the turn produced
no aggregated guidance. These tests pin the four behaviours that keep one bad seat
from eating a run:

* **partial quorum** — >=1 successful reference is enough; survivors are aggregated
  and the unavailable ones are named in the guidance block;
* **fail fast on deterministic errors** — a 5xx that merely *wraps* an upstream 4xx
  is not retried (it would fail identically and only add latency), while a genuine
  5xx still is;
* **circuit breaker** — N consecutive failures bench a backend for a cooldown, the
  breaker logs on trip and reset, and a recovered backend closes it;
* **observability** — every reference reports ok / failure_class / status_code /
  latency_ms so a sick seat is visible before it degrades a whole run.
"""

import logging

import pytest

from agent.auxiliary_client import _is_transient_transport_error
from agent.error_classifier import classify_api_error, wraps_deterministic_rejection
from agent.moa_loop import (
    _BREAKER_FAILURE_THRESHOLD,
    MoAChatCompletions,
    _classify_reference_failure,
    _guidance_inputs,
    _RefAccounting,
    _run_reference,
    reset_reference_breakers,
)
from agent.moa_trace import slot_metrics
from agent.usage_pricing import CanonicalUsage

# The exact production failure: the shim caught Vertex's 400 and re-raised it as its own 500.
PROD_ERROR_TEXT = (
    "Error code: 500 - {'type': 'error', 'error': {'type': 'api_error', 'message': "
    "\"Error code: 400 - [{'error': {'code': 400, 'message': 'Request contains an invalid "
    "argument.', 'status': 'INVALID_ARGUMENT'}}]\"}}"
)


class FakeAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture(autouse=True)
def _clean_breakers():
    """Breaker state is process-global; isolate every test."""
    reset_reference_breakers()
    yield
    reset_reference_breakers()


def _slot(model, provider="vertex-shim"):
    return {"provider": provider, "model": model}


def _ok(label="vertex-shim:claude-sonnet-5", text="prefer the cached advisory view"):
    return (label, text, _RefAccounting(CanonicalUsage(), ok=True, latency_ms=120))


def _failed(label="vertex-shim:claude-opus-5", text=f"[failed: {PROD_ERROR_TEXT}]"):
    return (label, text, _RefAccounting(CanonicalUsage(), ok=False, failure_class="4xx", status_code=500))


def _facade():
    """A facade without __init__ side effects (the pattern the existing MoA tests use)."""
    f = MoAChatCompletions.__new__(MoAChatCompletions)
    f.preset_name = "default"
    f._privacy_mode = ""
    return f


class TestPartialQuorum:
    """>=1 successful reference is a usable fan-out."""

    def test_one_success_one_failure_aggregates_over_the_survivor(self):
        refs, degraded, all_failed = _guidance_inputs([_ok(), _failed()], False, "loud")

        assert [label for label, _, _ in refs] == ["vertex-shim:claude-sonnet-5"]
        assert not all_failed
        assert "vertex-shim:claude-opus-5" in degraded

    def test_guidance_names_the_unavailable_backend(self):
        guidance = _facade()._build_guidance(
            [_ok(), _failed()], _slot("claude-opus-5"), "loud")

        # The survivor's advice is present...
        assert "prefer the cached advisory view" in guidance
        # ...and the aggregator is told which seat it did NOT hear from.
        assert "Reference models unavailable: vertex-shim:claude-opus-5" in guidance
        # The failed advisor's raw error must not be replayed as if it were advice.
        assert "INVALID_ARGUMENT" not in guidance

    def test_silent_policy_suppresses_the_notice_but_keeps_the_survivor(self):
        refs, degraded, all_failed = _guidance_inputs([_ok(), _failed()], False, "silent")

        assert [label for label, _, _ in refs] == ["vertex-shim:claude-sonnet-5"]
        assert degraded == ""
        assert not all_failed

    def test_all_failed_is_flagged_so_the_aggregator_is_skipped(self):
        # Every reference down => fail fast (don't synthesize over nothing).
        refs, _degraded, all_failed = _guidance_inputs([_failed(), _failed()], False, "loud")

        assert refs == []
        assert all_failed

    def test_all_failed_guidance_is_actionable_not_empty(self):
        guidance = _facade()._build_guidance(
            [_failed(), _failed()], _slot("claude-opus-5"), "loud")

        assert "All reference models failed" in guidance
        assert "Act on your own judgment" in guidance


class TestFailFastOnDeterministicErrors:
    """A 5xx wrapping an upstream 4xx must not be retried."""

    def test_production_error_is_recognised_as_deterministic(self):
        assert wraps_deterministic_rejection(PROD_ERROR_TEXT)

    def test_production_error_is_not_retried(self):
        # Regression: a status-only check reads 500 and burns the whole retry budget
        # to reach an identical 400.
        assert not _is_transient_transport_error(FakeAPIError(PROD_ERROR_TEXT, 500))

    def test_production_error_classifies_as_non_retryable(self):
        verdict = classify_api_error(
            FakeAPIError(PROD_ERROR_TEXT, 500), provider="vertex-shim", model="claude-opus-5")

        assert not verdict.retryable

    @pytest.mark.parametrize("text,status", [
        ("Error code: 503 - upstream overloaded", 503),
        ("Error code: 502 - bad gateway", 502),
        ("Error code: 500 - internal error", 500),
        ("Error code: 500 - {'status': 'UNAVAILABLE'}", 500),
        ("Error code: 500 - {'status': 'DEADLINE_EXCEEDED'}", 500),
        ("Error code: 500 - {'status': 'RESOURCE_EXHAUSTED'}", 500),
        # A wrapped 429/408 is still genuinely worth retrying.
        ("Error code: 500 - 'Error code: 429 - rate limited'", 500),
        ("Error code: 500 - 'Error code: 408 - timed out'", 500),
    ])
    def test_genuine_transient_errors_still_retry(self, text, status):
        assert _is_transient_transport_error(FakeAPIError(text, status))

    @pytest.mark.parametrize("text", [
        "Error code: 500 - \"Error code: 400 - INVALID_ARGUMENT\"",
        "Error code: 500 - 'Error code: 404 - model not found'",
        "Error code: 500 - {'status': 'FAILED_PRECONDITION'}",
        "Error code: 500 - {'status': 'UNIMPLEMENTED'}",
    ])
    def test_wrapped_deterministic_errors_do_not_retry(self, text):
        assert not _is_transient_transport_error(FakeAPIError(text, 500))


class TestFailureClassification:
    """Per-backend failure buckets for the dashboard."""

    @pytest.mark.parametrize("text,status,expected", [
        (PROD_ERROR_TEXT, 500, "4xx"),          # wrapped-deterministic => 4xx, not "flaky server"
        ("Error code: 400 - bad request", 400, "4xx"),
        ("Error code: 500 - internal", 500, "5xx"),
        ("Error code: 503 - overloaded", 503, "5xx"),
        ("request timed out", 408, "timeout"),
        ("connection refused", None, "transport"),
    ])
    def test_failure_class(self, text, status, expected):
        assert _classify_reference_failure(FakeAPIError(text, status))[0] == expected


class TestCircuitBreaker:
    """A repeatedly-failing backend gets benched instead of re-dialed every turn."""

    def _always_fails(self, monkeypatch, exc=None):
        calls = []

        def _boom(**kwargs):
            calls.append(kwargs)
            raise exc or FakeAPIError(PROD_ERROR_TEXT, 500)

        monkeypatch.setattr("agent.moa_loop.call_llm", _boom)
        monkeypatch.setattr("agent.moa_loop._slot_runtime", lambda slot: dict(slot))
        return calls

    def test_trips_after_threshold_and_stops_dialing(self, monkeypatch):
        calls = self._always_fails(monkeypatch)
        slot = _slot("claude-opus-5")
        msgs = [{"role": "user", "content": "hi"}]

        for _ in range(_BREAKER_FAILURE_THRESHOLD):
            _run_reference(slot, msgs)
        assert len(calls) == _BREAKER_FAILURE_THRESHOLD

        # Breaker now open: the backend is not contacted again.
        label, text, acct = _run_reference(slot, msgs)
        assert len(calls) == _BREAKER_FAILURE_THRESHOLD, "breaker did not short-circuit the dial"
        assert "circuit breaker open" in text
        assert acct.failure_class == "circuit_open"

    def test_tripped_slot_still_reads_as_an_unavailable_reference(self, monkeypatch):
        """The breaker must reuse the existing degraded path, not invent a new one."""
        self._always_fails(monkeypatch)
        slot = _slot("claude-opus-5")
        msgs = [{"role": "user", "content": "hi"}]
        for _ in range(_BREAKER_FAILURE_THRESHOLD + 1):
            out = _run_reference(slot, msgs)

        # A tripped slot alongside a healthy one is a partial-quorum success.
        refs, degraded, all_failed = _guidance_inputs([_ok(), out], False, "loud")
        assert [label for label, _, _ in refs] == ["vertex-shim:claude-sonnet-5"]
        assert not all_failed
        assert "claude-opus-5" in degraded

    def test_trip_is_logged(self, monkeypatch, caplog):
        self._always_fails(monkeypatch)
        slot = _slot("claude-opus-5")
        with caplog.at_level(logging.WARNING, logger="agent.moa_loop"):
            for _ in range(_BREAKER_FAILURE_THRESHOLD):
                _run_reference(slot, [{"role": "user", "content": "hi"}])

        assert any("circuit breaker TRIPPED" in r.getMessage() for r in caplog.records)

    def test_reset_is_logged(self, monkeypatch, caplog):
        self._always_fails(monkeypatch)
        slot = _slot("claude-opus-5")
        for _ in range(_BREAKER_FAILURE_THRESHOLD):
            _run_reference(slot, [{"role": "user", "content": "hi"}])

        base = __import__("time").monotonic()
        monkeypatch.setattr("agent.moa_loop.time.monotonic", lambda: base + 10_000)
        with caplog.at_level(logging.INFO, logger="agent.moa_loop"):
            _run_reference(slot, [{"role": "user", "content": "hi"}])

        assert any("circuit breaker RESET" in r.getMessage() for r in caplog.records)

    def test_breaker_is_per_backend(self, monkeypatch):
        """Benching one seat must not bench a healthy sibling."""
        calls = self._always_fails(monkeypatch)
        msgs = [{"role": "user", "content": "hi"}]
        for _ in range(_BREAKER_FAILURE_THRESHOLD + 1):
            _run_reference(_slot("claude-opus-5"), msgs)
        before = len(calls)

        _run_reference(_slot("claude-sonnet-5"), msgs)
        assert len(calls) == before + 1, "a different backend was wrongly short-circuited"

    def test_cooldown_expiry_reopens_the_backend(self, monkeypatch):
        calls = self._always_fails(monkeypatch)
        slot = _slot("claude-opus-5")
        msgs = [{"role": "user", "content": "hi"}]
        for _ in range(_BREAKER_FAILURE_THRESHOLD):
            _run_reference(slot, msgs)
        tripped_at = len(calls)

        # Jump past the cooldown window.
        base = __import__("time").monotonic()
        monkeypatch.setattr("agent.moa_loop.time.monotonic", lambda: base + 10_000)

        _run_reference(slot, msgs)
        assert len(calls) == tripped_at + 1, "breaker never reset after its cooldown"


class TestFlappingBackend:
    """An intermittently-failing backend must not be benched by isolated blips."""

    def test_success_resets_the_failure_streak(self, monkeypatch):
        outcomes = iter([
            "fail", "fail", "ok",      # streak broken before the threshold
            "fail", "fail", "ok",      # and again
            "fail", "fail", "ok",
        ])
        calls = []

        class _Resp:
            choices = []
            usage = None

        def _flaky(**kwargs):
            calls.append(kwargs)
            if next(outcomes) == "fail":
                raise FakeAPIError("Error code: 503 - overloaded", 503)
            return _Resp()

        monkeypatch.setattr("agent.moa_loop.call_llm", _flaky)
        monkeypatch.setattr("agent.moa_loop._slot_runtime", lambda slot: dict(slot))
        monkeypatch.setattr("agent.moa_loop._extract_text", lambda r: "advice")

        slot = _slot("claude-opus-5")
        msgs = [{"role": "user", "content": "hi"}]
        for _ in range(9):
            _run_reference(slot, msgs)

        # 9 dials, none short-circuited: a flapping backend stays in rotation because
        # the breaker counts CONSECUTIVE failures, not cumulative ones.
        assert len(calls) == 9


class TestObservability:
    """Per-reference health must reach the metrics hook."""

    def test_metrics_carry_health_fields(self):
        acct = _RefAccounting(
            CanonicalUsage(), ok=False, failure_class="4xx", status_code=500, latency_ms=87)
        m = slot_metrics(acct, "vertex-shim:claude-opus-5", output="[failed: ...]")

        assert m["ok"] is False
        assert m["failure_class"] == "4xx"
        assert m["status_code"] == 500
        assert m["latency_ms"] == 87

    def test_successful_reference_records_ok_and_latency(self, monkeypatch):
        class _Resp:
            choices = []
            usage = None

        monkeypatch.setattr("agent.moa_loop.call_llm", lambda **k: _Resp())
        monkeypatch.setattr("agent.moa_loop._slot_runtime", lambda slot: dict(slot))
        monkeypatch.setattr("agent.moa_loop._extract_text", lambda r: "advice")

        _label, _text, acct = _run_reference(_slot("claude-sonnet-5"), [{"role": "user", "content": "hi"}])

        assert acct.ok is True
        assert acct.failure_class is None
        assert isinstance(acct.latency_ms, int)

    def test_failed_reference_records_class_and_status(self, monkeypatch):
        def _boom(**kwargs):
            raise FakeAPIError(PROD_ERROR_TEXT, 500)

        monkeypatch.setattr("agent.moa_loop.call_llm", _boom)
        monkeypatch.setattr("agent.moa_loop._slot_runtime", lambda slot: dict(slot))

        _label, _text, acct = _run_reference(_slot("claude-opus-5"), [{"role": "user", "content": "hi"}])

        assert acct.ok is False
        assert acct.failure_class == "4xx"   # wrapped-deterministic, not "5xx"
        assert acct.status_code == 500
