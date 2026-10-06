"""Terminal failure statuses are suppressed on chat surfaces — one failure bubble per failed turn.

E0919 duplicate-failure render: ``max_retries_exhausted_result`` emits a terminal
status ("❌ Rate limited after N retries — …") and the turn then delivers the
authoritative final copy (``exhausted_copy`` / billing label). Converting that
status through the provider-error reply table rendered a SECOND failure bubble
beside the final. The status is redundant by construction — the final always
follows in the same failed turn — so on messaging surfaces it must return None.

Regression for the E0919 duplicate-failure render (replay receipts
slack-rate-limit-replay-2026{0919,0920,0921}.json under ~/.shadow/plans/hermes-agent/evidence).
"""

import pytest

from gateway.run import (
    _prepare_gateway_status_message,
    _sanitize_gateway_final_response,
)

# Every human-facing chat surface (same rationale as test_telegram_noise_filter.py:
# the seam is platform-agnostic shared logic; a representative subset suffices).
CHAT_PLATFORMS = ["telegram", "slack", "feishu"]

# The four terminal statuses emitted by agent/turn_recovery.max_retries_exhausted_result.
TERMINAL_FAILURE_STATUSES = [
    "❌ Rate limited after 3 retries — HTTP 429: Rate limit reached for requests",
    "❌ API failed after 3 retries — upstream connect error",
    "❌ Billing or credits exhausted — HTTP 402 quota exhausted",
    "❌ Provider reported usage/credit exhaustion (unverified — may be a "
    "content-filter rejection) — HTTP 402",
]


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize("status", TERMINAL_FAILURE_STATUSES)
def test_terminal_failure_status_suppressed_on_chat_surfaces(platform, status):
    # The turn's final_response (exhausted_copy / billing label) is the ONE user-facing
    # failure message; the terminal status must not render beside it as a second bubble.
    assert _prepare_gateway_status_message(platform, "warn", status) is None


@pytest.mark.parametrize("platform", ["local", "api_server"])
@pytest.mark.parametrize("status", TERMINAL_FAILURE_STATUSES)
def test_terminal_failure_status_kept_on_local_surfaces(platform, status):
    # Local/CLI surfaces keep the raw diagnostic stream.
    assert _prepare_gateway_status_message(platform, "warn", status) == status


def test_exhausted_final_copy_still_renders_alone():
    # The final copy is the turn's single failure message: it survives the sanitizer
    # byte-for-byte (never collapsed into the generic short reply).
    from agent.turn_failure_copy import exhausted_copy

    final = exhausted_copy(
        "rate_limit", label="Z.AI / GLM", attempts=3,
        summary="HTTP 429: Rate limit reached for requests",
    )
    assert _sanitize_gateway_final_response("slack", final) == final


def test_transient_provider_error_status_still_converts():
    # A raw provider envelope that is NOT one of the four terminal statuses (no
    # terminal emitter produces this shape as a status) still gets the short
    # user-safe reply — suppression must not swallow the whole reply table.
    raw = "API call failed after 3 retries: HTTP 429: Rate limit reached for requests"
    reply = _prepare_gateway_status_message("slack", "warn", raw)
    assert reply == (
        "⏱️ The AI model service is rate-limiting requests. Wait a moment, then use /retry."
    )
