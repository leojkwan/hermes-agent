"""Explicit per-job Fast opt-in; an unset job never inherits interactive Fast."""

from typing import Any

from agent.fast_mode import STATIC_TIERS, parse_exact_service_tier


def normalize_job_service_tier(value: Any) -> str | None:
    """None leaves cron unchanged; an empty string explicitly selects normal."""
    return None if value is None else parse_exact_service_tier(value)


def cron_service_tier(job: dict, runtime: dict, model: str) -> dict:
    from hermes_cli.models import resolve_fast_mode_overrides
    from tools.delegate_tool_tuning import without_service_tier

    tier = normalize_job_service_tier(job.get("service_tier"))
    overrides = dict(runtime.get("request_overrides") or {})
    if tier is not None:
        overrides = without_service_tier(overrides)
        if tier in STATIC_TIERS:
            overrides.update(
                resolve_fast_mode_overrides(
                    model,
                    provider=runtime.get("provider"),
                    base_url=runtime.get("base_url"),
                    tier=tier,
                )
                or {}
            )
    return {"service_tier": tier or None, "request_overrides": overrides or None}


def pin_cron_service_tier(agent, job: dict):
    """Construction merges provider extras; reapply only the explicit job's tier."""
    if job.get("service_tier") is not None:
        agent.request_overrides = (
            cron_service_tier(
                job,
                {
                    "provider": agent.provider,
                    "base_url": agent.base_url,
                    "request_overrides": agent.request_overrides,
                },
                agent.model,
            )["request_overrides"]
            or {}
        )
    return agent
