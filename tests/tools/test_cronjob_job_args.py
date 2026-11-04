"""Tests for tools/cronjob_job_args.py::_validate_cron_script_path (issue #105761).

Regression coverage: the validator used to accept a script path that pointed
at a file that doesn't exist, only failing later at every cron fire with a
generic "Script not found" error from cron/scheduler_script.py. It also
hardcoded "~/.hermes/scripts/" in its messages even though resolution goes
through get_hermes_home(), which is per-profile.
"""

from hermes_constants import get_hermes_home
from tools.cronjob_job_args import _validate_cron_script_path


class TestValidateCronScriptPath:
    def test_missing_script_file_is_rejected_at_creation_time(self):
        error = _validate_cron_script_path("does_not_exist.sh")
        assert error is not None
        assert "not found" in error.lower()

    def test_existing_script_file_passes(self):
        scripts_dir = get_hermes_home() / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "real.sh").write_text("#!/bin/sh\necho hi\n")
        assert _validate_cron_script_path("real.sh") is None

    def test_missing_file_error_names_the_resolved_scripts_dir(self):
        # The resolved dir must appear literally so the message stays correct
        # under profiles, where get_hermes_home() is not the global ~/.hermes.
        scripts_dir = get_hermes_home() / "scripts"
        error = _validate_cron_script_path("missing.py")
        assert str(scripts_dir) in error

    def test_absolute_path_error_names_the_resolved_scripts_dir_not_global_literal(self):
        scripts_dir = get_hermes_home() / "scripts"
        error = _validate_cron_script_path("/etc/passwd")
        assert str(scripts_dir) in error


class TestCronServiceTier:
    def test_registered_create_update_readback_keeps_tier_opt_in(
        self, tmp_path, monkeypatch
    ):
        import json

        from cron import jobs
        from tools import cronjob_tools

        monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron/jobs.json")
        monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron/output")

        def dispatch(**args):
            return json.loads(cronjob_tools.registry.dispatch("cronjob_manage", args))

        original = dispatch(
            action="create",
            prompt="Check accepted work",
            schedule="every 1h",
            deliver="local",
        )
        original_id = original["job_id"]
        assert "service_tier" not in jobs.get_job(original_id)
        fast = dispatch(
            action="create",
            prompt="Check accepted work",
            schedule="every 1h",
            deliver="local",
            service_tier="Fast",
        )
        fast_id = fast["job_id"]
        assert jobs.get_job(fast_id)["service_tier"] == "priority"
        assert dispatch(action="update", job_id=fast_id, name="renamed")["success"]
        assert jobs.get_job(fast_id)["service_tier"] == "priority"
        for word, stored in (
            ("normal", ""),
            ("auto", "auto"),
            ("cold", "cold"),
            ("ultra", "ultrafast"),
        ):
            assert dispatch(action="update", job_id=fast_id, service_tier=word)[
                "success"
            ]
            assert jobs.get_job(fast_id)["service_tier"] == stored
            listed = dispatch(action="list", include_disabled=True)["jobs"]
            assert (
                next(j for j in listed if j["job_id"] == fast_id)["service_tier"]
                == stored
            )
        before = jobs.load_jobs()
        invalid = dispatch(action="update", job_id=fast_id, service_tier="warp9")
        assert invalid["success"] is False
        assert jobs.load_jobs() == before
        listed = dispatch(action="list", include_disabled=True)["jobs"]
        assert (
            next(j for j in listed if j["job_id"] == fast_id)["service_tier"]
            == "ultrafast"
        )
        assert "service_tier" not in jobs.get_job(original_id)

    def test_store_validates_tier_and_preserves_null_without_upgrade(
        self, tmp_path, monkeypatch
    ):
        import pytest

        from cron import jobs
        from cron.job_definition import merge_job_definition

        monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron/jobs.json")
        monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron/output")
        job = jobs.create_job(
            prompt="Check accepted work", schedule="every 1h", service_tier=None
        )
        assert "service_tier" not in jobs.get_job(job["id"])
        for invalid in ("warp9", {"tier": "Fast"}):
            with pytest.raises(ValueError):
                jobs.create_job(
                    prompt="Check accepted work",
                    schedule="every 1h",
                    service_tier=invalid,
                )
            with pytest.raises(ValueError):
                jobs.update_job(job["id"], {"service_tier": invalid})
        assert len(jobs.load_jobs()) == 1
        assert (
            jobs.update_job(job["id"], {"service_tier": " Fast "})["service_tier"]
            == "priority"
        )
        assert (
            jobs.update_job(job["id"], {"service_tier": None})["service_tier"] is None
        )
        merged = merge_job_definition({**job, "service_tier": "priority"}, job)
        assert "service_tier" not in merged
