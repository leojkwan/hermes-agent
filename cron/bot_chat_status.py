"""Read-only job-list projection of Bot Chat receipts that settled after the run."""

from __future__ import annotations

from typing import Any


def project_delivery_status(job: dict[str, Any]) -> None:
    """Refresh the returned listing, never the job store or the delivery itself.

    Cron books an asynchronous Bot Chat admission before its recipient finishes.
    Its saved queued snapshot can outlive the exact destination receipt. Missing,
    malformed or unreadable receipts stay unconfirmed; listing must never resend.
    """
    queued = job.get("last_delivery_queued")
    if not isinstance(queued, dict) or not queued:
        return
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_profile_dir
    from tools.bot_live_delivery import read_delivery_result

    pending = dict(queued)
    failures = []
    for target, saved in queued.items():
        if not isinstance(target, str) or not target.startswith("bot-chat:"):
            continue
        if not isinstance(saved, dict) or not saved.get("delivery_id"):
            continue
        try:
            profile = target.removeprefix("bot-chat:")
            home = (
                get_hermes_home() if profile == "(own)" else get_profile_dir(profile)
            ).resolve()
            receipt = read_delivery_result(home, saved["delivery_id"])
            if (
                not isinstance(receipt, dict)
                or receipt.get("delivery_id") != saved["delivery_id"]
                or receipt.get("profile_home") != str(home)
            ):
                continue
        except (OSError, ValueError, TypeError, RuntimeError):
            continue
        status = receipt.get("status")
        if not isinstance(status, str):
            continue
        if status == "settled" and receipt.get("admission_id"):
            pending.pop(target)
        elif status in {"failed", "cancelled", "ambiguous"}:
            pending.pop(target)
            failures.append(f"{target}: {status}; delivery not confirmed")

    job["last_delivery_queued"] = pending or None
    if failures:
        errors = [job["last_delivery_error"]] if job.get("last_delivery_error") else []
        job["last_delivery_error"] = "; ".join([*errors, *failures])
        if job.get("last_status") in {"ok", "delivery_queued"}:
            job["last_status"] = "delivery_failed"
    elif not pending and job.get("last_status") == "delivery_queued":
        job["last_status"] = (
            "delivery_failed" if job.get("last_delivery_error") else "ok"
        )
