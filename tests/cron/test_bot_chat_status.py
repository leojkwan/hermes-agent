"""Listing reconciles exact destination receipts without changing or repeating work."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from cron import jobs
from cron.bot_chat_status import project_delivery_status
from tools import bot_live_delivery as mailbox


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "source"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(jobs, "CRON_DIR", home / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", home / "cron/jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", home / "cron/output")
    monkeypatch.setattr(
        mailbox, "authority_delivery", Mock(side_effect=AssertionError("no send"))
    )
    monkeypatch.setattr(
        mailbox,
        "find_canonical_live_owner",
        Mock(side_effect=AssertionError("no discovery")),
    )
    return home


def receipt(home, key, status, **changes):
    record = dict(
        delivery_id=key,
        profile_home=str(home.resolve()),
        admission_id="admission",
        session_id="retained-hand",
        status=status,
        message="synthetic",
        reply="done",
    )
    record.update(changes)
    with mailbox._locked(home) as root:
        mailbox._write(root / f"{key}.json", record)


def pending_job(key="a" * 64, target="bot-chat:(own)"):
    return dict(
        id="rollup",
        last_status="ok",
        last_delivery_queued={target: dict(status="queued", delivery_id=key)},
    )


@pytest.mark.parametrize(
    "target", ["bot-chat:(own)", "bot-chat:default", "bot-chat:other"]
)
def test_listing_uses_exact_settled_receipt_and_does_not_save(store, target, capsys):
    from argparse import Namespace
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.cron import cron_command

    job = jobs.create_job(prompt="Synthetic roll-up", schedule="every 2h")
    saved = jobs.load_jobs()
    key = "a" * 64
    saved[0].update(pending_job(key, target), id=job["id"])
    jobs.save_jobs(saved)
    before = jobs.JOBS_FILE.read_bytes()
    name = target.split(":", 1)[1]
    destination = store if name == "(own)" else get_profile_dir(name)
    receipt(destination, key, "settled")

    listed = jobs.list_jobs(include_disabled=True)[0]
    assert listed["last_delivery_queued"] is None
    assert listed["last_status"] == "ok"
    assert jobs.JOBS_FILE.read_bytes() == before
    assert jobs.load_jobs()[0]["last_delivery_queued"]  # only the listing changed
    cron_command(Namespace(cron_command="list", all=True, json=False))
    output = capsys.readouterr().out
    assert job["id"] in output
    assert "Delivery still in progress" not in output
    assert jobs.JOBS_FILE.read_bytes() == before
    mailbox.authority_delivery.assert_not_called()
    mailbox.find_canonical_live_owner.assert_not_called()


@pytest.mark.parametrize(
    "status", ["queued", "claimed", "unknown", ["settled"], {"bad": "status"}, None]
)
def test_unconfirmed_outcomes_remain_pending(store, status):
    job = pending_job()
    if status is not None:
        receipt(store, "a" * 64, status)
    project_delivery_status(job)
    assert job["last_delivery_queued"]["bot-chat:(own)"]["status"] == "queued"


@pytest.mark.parametrize(
    "changes",
    [
        {"delivery_id": "b" * 64},
        {"profile_home": "/wrong-profile"},
        {"admission_id": ""},
    ],
)
def test_unbound_receipt_cannot_confirm_delivery(store, changes):
    job = pending_job()
    receipt(store, "a" * 64, "settled", **changes)
    project_delivery_status(job)
    assert job["last_delivery_queued"]


@pytest.mark.parametrize("status", ["failed", "cancelled", "ambiguous"])
def test_terminal_failure_is_visible_instead_of_pending(store, status):
    job = pending_job()
    receipt(store, "a" * 64, status)
    project_delivery_status(job)
    assert job["last_delivery_queued"] is None
    assert job["last_status"] == "delivery_failed"
    assert status in job["last_delivery_error"]
    assert "not confirmed" in job["last_delivery_error"]


def test_explicit_never_admitted_refusal_is_a_failure(store):
    job = pending_job()
    receipt(store, "a" * 64, "failed", admission_id="", refused=True)
    project_delivery_status(job)
    assert job["last_delivery_queued"] is None
    assert job["last_status"] == "delivery_failed"


def test_settled_receipt_does_not_hide_independent_failure(store):
    job = pending_job()
    job.update(last_status="error", last_delivery_error="slack timeout")
    receipt(store, "a" * 64, "settled")
    project_delivery_status(job)
    assert job["last_status"] == "error"
    assert job["last_delivery_error"] == "slack timeout"


def test_mixed_destinations_keep_pending_and_preserve_independent_error(store):
    job = pending_job()
    job["last_delivery_queued"]["bot-chat:other"] = dict(
        status="queued", delivery_id="b" * 64
    )
    job["last_delivery_error"] = "slack timeout"
    receipt(store, "a" * 64, "settled")
    project_delivery_status(job)
    assert set(job["last_delivery_queued"]) == {"bot-chat:other"}
    assert job["last_delivery_error"] == "slack timeout"


@pytest.mark.parametrize("error", [PermissionError, RuntimeError])
def test_malformed_or_unreadable_receipt_cannot_hide_pending(store, monkeypatch, error):
    job = pending_job()
    with mailbox._locked(store) as root:
        (root / f"{'a' * 64}.json").write_text(
            json.dumps(["not-a-record"]), encoding="utf-8"
        )
    project_delivery_status(job)
    assert job["last_delivery_queued"]
    monkeypatch.setattr(
        mailbox, "read_delivery_result", Mock(side_effect=error("unreadable"))
    )
    project_delivery_status(job)
    assert job["last_delivery_queued"]


def test_listing_does_not_recreate_missing_target_profile(store):
    from hermes_cli.profiles import get_profile_dir

    destination = get_profile_dir("missing")
    job = pending_job(target="bot-chat:missing")
    assert not destination.exists()
    project_delivery_status(job)
    assert job["last_delivery_queued"]
    assert not destination.exists()
