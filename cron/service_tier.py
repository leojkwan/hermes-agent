"""Explicit per-job Fast opt-in; an unset job never inherits interactive Fast."""

from typing import Any

from agent.fast_mode import ServiceTierPolicy, parse_exact_service_tier


def normalize_job_service_tier(value: Any) -> str | None:
    """None leaves cron unchanged; an empty string explicitly selects normal."""
    return None if value is None else parse_exact_service_tier(value)


def cron_service_tier(job: dict, runtime: dict) -> dict:
    tier = normalize_job_service_tier(job.get("service_tier"))
    return {
        "service_tier": tier or None,
        "request_overrides": dict(runtime.get("request_overrides") or {}) or None,
    }


def bind_cron_service_tier(agent, job: dict):
    """Keep the explicit choice separate from route snapshots and provider override merges."""
    tier = normalize_job_service_tier(job.get("service_tier"))
    if tier is not None:
        agent._service_tier_policy = ServiceTierPolicy(tier)
    return agent
