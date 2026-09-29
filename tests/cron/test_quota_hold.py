"""Provider quota windows park a cron job instead of re-firing into them (#89376;
ai-velho-oy/chairman#3).

Contract (cron/quota_hold.py): a failed run whose cause is a rate-limited ``AuthError`` with a
``retry after <N>s`` hint parks a recurring job's ``next_run_at`` past the window and stamps
``quota_hold_until``; the stale-error re-arm leaves a held job alone; a run that reaches the
model clears the marker. The hint is read only from the AuthError in the cause chain, never from
arbitrary failure text.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import cron.scheduler as sched
from cron import quota_hold as qh
from cron.jobs import (
    _job_is_stale_error_recurring, claim_job_for_fire, create_job, get_due_jobs, get_job,
    mark_job_run, update_job,
)
from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError
from hermes_cli import env_loader

QUOTA_MSG = "Codex provider quota exhausted (429); retry after 123518s. Credentials are still valid."


@pytest.fixture
def tmp_cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _quota_error() -> AuthError:
    return AuthError(QUOTA_MSG, provider="openai-codex", code=CODEX_RATE_LIMITED_CODE)


def test_hold_seconds_only_from_rate_limited_auth_error_in_cause_chain():
    """The scheduler wraps the resolve failure in a RuntimeError ``from`` the AuthError; the
    hint survives through the cause chain, and text alone (or a re-login AuthError) never
    parks a job."""
    try:
        raise RuntimeError(QUOTA_MSG) from _quota_error()
    except RuntimeError as wrapped:
        assert qh.hold_seconds_from_failure(wrapped) == 123518.0

    assert qh.hold_seconds_from_failure(RuntimeError(QUOTA_MSG)) is None
    assert qh.hold_seconds_from_failure(RuntimeError("HTTP 429: retry after 60s")) is None
    relogin = AuthError(QUOTA_MSG, provider="openai-codex", code="expired", relogin_required=True)
    assert qh.hold_seconds_from_failure(relogin) is None
    structured = AuthError("quota", code=CODEX_RATE_LIMITED_CODE, retry_after=900)
    assert qh.hold_seconds_from_failure(structured) == 900.0


def test_weekly_cron_retries_when_quota_recovers_before_next_occurrence(
    tmp_cron_home, monkeypatch,
):
    """A weekly fire blocked by a shorter quota window retries when the provider reopens;
    it is not silently deferred until the following week's natural occurrence."""
    now = datetime(2026, 9, 18, 12, 1, tzinfo=timezone.utc)
    natural_next = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    monkeypatch.setattr(qh, "_hermes_now", lambda: now)
    job = create_job("weekly digest", "0 12 * * 5")
    assert datetime.fromisoformat(job["next_run_at"]) == natural_next

    assert mark_job_run(
        job["id"], False, QUOTA_MSG, quota_hold_seconds=20 * 60 * 60,
        recover_consumed_fire=True,
    )

    held = get_job(job["id"])
    assert held is not None
    retry_at = datetime.fromisoformat(held["next_run_at"])
    assert retry_at == now + timedelta(hours=20, seconds=qh.HOLD_SLACK_SECONDS) < natural_next
    assert qh.is_recovery_fire(held, held["next_run_at"])
    edited = {**held, "schedule": {"kind": "cron", "expr": "0 9 * * *"}}
    assert not qh.is_recovery_fire(edited, edited["next_run_at"])

    monkeypatch.setattr("cron.jobs._hermes_now", lambda: retry_at + timedelta(seconds=1))
    assert job["id"] in {due["id"] for due in get_due_jobs()}


def test_recovery_fire_skips_dense_schedules_and_never_re_parks(monkeypatch):
    """Recovery is one attempt for sparse schedules only: an hourly job whose hold ends two
    minutes before :00 keeps its natural :00 (no off-lattice near-duplicate), and a job that is
    already the recovery fire (carries quota_hold_until) failing again is not re-parked."""
    now = datetime(2026, 9, 18, 12, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(qh, "_hermes_now", lambda: now)

    dense = {
        "schedule": {"kind": "cron", "expr": "0 * * * *"},
        "next_run_at": datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc).isoformat(),
    }
    natural = dense["next_run_at"]
    assert not qh.plan_hold(dense, hold_seconds=57 * 60 - qh.HOLD_SLACK_SECONDS,
                            recover_consumed_fire=True)
    assert dense["next_run_at"] == natural
    assert qh.STATE_KEY not in dense

    held_again = {
        "schedule": {"kind": "cron", "expr": "0 12 * * 5"},
        "next_run_at": (now + timedelta(days=7)).isoformat(),
        qh.STATE_KEY: now.isoformat(),
        qh.SCHEDULE_EXPR_KEY: "0 12 * * 5",
    }
    natural = held_again["next_run_at"]
    assert not qh.plan_hold(held_again, hold_seconds=20 * 60 * 60, recover_consumed_fire=True)
    assert held_again["next_run_at"] == natural
    assert qh.STATE_KEY not in held_again


def _raise_quota(**_kw):
    raise _quota_error()


def _tick(job, home, deliveries, resolve):
    """One real scheduler tick (preflight ON) with the provider resolver replaced by *resolve*."""
    with patch("cron.scheduler._hermes_home", home), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch.object(env_loader, "load_hermes_dotenv"), \
         patch.object(env_loader, "reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve), \
         patch.object(sched, "_deliver_result",
                      side_effect=lambda jb, content, **kw: deliveries.append(content)), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.side_effect = RuntimeError("model said no")
        sched.run_one_job(dict(job))


def test_classified_rate_limit_result_parks_real_scheduled_fire_once(tmp_cron_home, monkeypatch):
    """A turn-time provider rate limit never reaches cron as an exception: the agent returns a
    classified result (``failed``, ``failure_reason="rate_limit"``, ``failure_resets_at``) and
    the scheduler converts it to a bare RuntimeError. The real scheduled fire must still park at
    the provider's reset, recover the consumed daily fire once, and not re-park when that
    recovery fire fails again. Anything short of a classified rate limit with a finite future
    numeric reset (other reasons, bool/NaN/inf/string/past resets, retry text in the agent's
    own output) keeps the natural schedule."""
    fire = datetime(2026, 9, 28, 12, 0, 56, tzinfo=timezone.utc)
    clock = {"now": fire - timedelta(minutes=5)}
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: clock["now"])
    monkeypatch.setattr(qh, "_hermes_now", lambda: clock["now"])
    reset = fire + timedelta(seconds=554)
    natural = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    def fire_once(job_id, result):
        claimed = claim_job_for_fire(job_id, return_job=True)
        assert claimed, "the due fire must be claimable"
        deliveries: list = []
        with patch("cron.scheduler._hermes_home", tmp_cron_home), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch.object(env_loader, "load_hermes_dotenv"), \
             patch.object(env_loader, "reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=MagicMock()), \
             patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
             patch.object(sched, "_deliver_result",
                          side_effect=lambda jb, content, **kw: deliveries.append(content)), \
             patch("run_agent.AIAgent") as agent_cls:
            agent_cls.return_value.run_conversation.return_value = result
            sched.run_one_job(claimed)
        return get_job(job_id), deliveries

    def failed(**fields):
        return {"failed": True, "completed": False, "final_response": "",
                "error": "Incomplete upstream response: You've hit your session limit",
                "failure_reason": "rate_limit", "failure_resets_at": reset.timestamp(), **fields}

    # Rejected shapes: each keeps the natural next occurrence and stamps no hold.
    rejected = [
        failed(failure_reason="billing"), failed(failure_reason="unknown"),
        failed(failure_reason=None), failed(failure_resets_at=True),
        failed(failure_resets_at=float("nan")), failed(failure_resets_at=float("inf")),
        failed(failure_resets_at=str(reset.timestamp())),
        failed(failure_resets_at=(fire - timedelta(seconds=5)).timestamp()),
        failed(failure_resets_at=None),
        failed(failure_resets_at=datetime(2400, 1, 1, tzinfo=timezone.utc).timestamp()),
        failed(failure_resets_at=1e12),
        {"failed": True, "completed": False, "failure_reason": "unknown",
         "error": "tool said: HTTP 429, retry after 60s",
         "final_response": "retry after 60s; resets_at 9999999999"},
    ]
    for i, result in enumerate(rejected):
        clock["now"] = fire
        assert qh.hold_seconds_from_result(result) is None, (i, result)
        clock["now"] = fire - timedelta(minutes=5)
        job = create_job(f"brief {i}", "0 12 * * *", deliver="local")
        clock["now"] = fire
        j, _ = fire_once(job["id"], result)
        assert j["last_status"] == "error", (i, j)
        assert datetime.fromisoformat(j["next_run_at"]) == natural, (i, result)
        assert qh.STATE_KEY not in j, (i, result)

    clock["now"] = fire - timedelta(minutes=5)
    job = create_job("morning letter", "0 12 * * *", deliver="local")
    clock["now"] = fire
    j, deliveries = fire_once(job["id"], failed())
    held_until = reset + timedelta(seconds=qh.HOLD_SLACK_SECONDS)
    assert j["last_status"] == "error"
    assert datetime.fromisoformat(j["next_run_at"]) == held_until < natural
    assert j[qh.STATE_KEY] == j["next_run_at"]
    assert qh.is_recovery_fire(j, j["next_run_at"])
    assert "_quota_hold_seconds" not in j
    assert len(deliveries) == 1 and "held" in deliveries[0], deliveries

    # The recovery fire is due once; failing again waits for the natural schedule (at most once).
    clock["now"] = held_until + timedelta(seconds=1)
    assert job["id"] in {d["id"] for d in get_due_jobs()}
    j, _ = fire_once(job["id"], failed(
        failure_resets_at=(clock["now"] + timedelta(seconds=554)).timestamp()))
    assert datetime.fromisoformat(j["next_run_at"]) == natural
    assert qh.STATE_KEY not in j


_RUNTIME = {"api_key": "test-key", "base_url": "https://example.invalid/v1",
            "provider": "openrouter", "api_mode": "chat_completions"}


def test_quota_hold_parks_past_window_survives_stale_rearm_and_clears_on_model_reach(tmp_cron_home):
    """A 30-minute job whose provider resolve raises the Codex quota AuthError ('retry after
    123518s') is parked by the real scheduler tick: preflight lets the rate-limited AuthError
    through (it is not a missing credential), the one delivered alert carries the hold notice,
    and the job does not fire again inside the window (not even after the stale-error re-arm's
    cadence+grace). The marker clears once a run reaches the model."""
    job = create_job("portfolio triage", "every 30m", deliver="local")
    job_id = job["id"]
    now = datetime.now(timezone.utc)
    deliveries: list = []

    _tick(get_job(job_id), tmp_cron_home, deliveries, _raise_quota)
    j = get_job(job_id)
    assert j["last_status"] == "error"
    assert len(deliveries) == 1, deliveries
    parked = datetime.fromisoformat(j["next_run_at"])
    assert parked - now >= timedelta(seconds=123518), "next_run_at must land past the window"
    assert j[qh.STATE_KEY] == j["next_run_at"]
    assert "_quota_hold_seconds" not in j

    # Two hours later the job looks like a wedged stale-error record (#62002) — the hold says
    # it is parked on purpose, so it is neither re-armed nor due.
    update_job(job_id, {"last_run_at": (now - timedelta(hours=2)).isoformat()})
    j = get_job(job_id)
    assert not _job_is_stale_error_recurring(j, j["schedule"], now)
    assert all(d["id"] != job_id for d in get_due_jobs())
    assert datetime.fromisoformat(get_job(job_id)["next_run_at"]) == parked

    # A run that reached the model (either outcome) clears the marker.
    assert mark_job_run(job_id, False, "RuntimeError: model said no")
    j = get_job(job_id)
    assert qh.STATE_KEY not in j
    assert datetime.fromisoformat(j["next_run_at"]) - now < timedelta(hours=1)

    # Editing the schedule recomputes next_run_at from the new cadence; the stale marker must
    # not linger on a record that is no longer parked where it says.
    assert mark_job_run(job_id, False, QUOTA_MSG, quota_hold_seconds=123518)
    assert qh.STATE_KEY in get_job(job_id)
    j = update_job(job_id, {"schedule": "every 15m"})
    assert qh.STATE_KEY not in j
    assert datetime.fromisoformat(j["next_run_at"]) - now < timedelta(hours=1)


@pytest.mark.parametrize("retry_after", [7 * 86400 + 1, 1e12, float("inf")])
def test_implausible_provider_wait_never_parks(retry_after):
    """A wait beyond any real provider window (weekly limits reset within 7 days) is corrupt data:
    parking on it would silence a job for years or overflow the hold notice."""
    err = AuthError("quota", provider="openai-codex", code=CODEX_RATE_LIMITED_CODE,
                    retry_after=retry_after)
    assert qh.hold_seconds_from_failure(err) is None
