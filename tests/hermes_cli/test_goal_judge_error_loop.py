"""A failed goal judge must not re-send the same terminal answer indefinitely."""

from unittest.mock import patch

import pytest


@pytest.fixture
def goal_home(tmp_path, monkeypatch):
    from hermes_cli import goals

    home = tmp_path / "goal-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def test_repeated_transport_error_pauses_across_reload_and_resume_rearms(goal_home):
    from hermes_cli import goals

    manager = goals.GoalManager("repeated-answer")
    manager.set("Prepare a report")
    failure = ("continue", "judge error: BadRequestError", False, None, True)
    with patch.object(goals, "judge_goal", return_value=failure):
        first = manager.evaluate_after_turn("The report is complete.")
        assert first["should_continue"] is True
        # The gateway/desktop can reinstantiate its manager between turns.
        manager = goals.GoalManager("repeated-answer")
        second = manager.evaluate_after_turn("The report is complete.")
        assert second["status"] == "paused"
        assert second["should_continue"] is False
        assert manager.state is not None and manager.state.paused_reason is not None
        assert "repeated" in manager.state.paused_reason.lower()
        assert goals.GoalManager("repeated-answer").evaluate_after_turn("The report is complete.")["should_continue"] is False
        manager.resume()
        assert manager.state is not None
        assert manager.state.consecutive_transport_failures == 0
        assert manager.evaluate_after_turn("The report is complete.")["should_continue"] is True


def test_live_work_does_not_count_as_duplicate_reply(goal_home):
    from hermes_cli import goals

    manager = goals.GoalManager("background-worker")
    manager.set("Wait for the worker and integrate")
    failure = ("continue", "judge error: TimeoutError", False, None, True)
    with patch.object(goals, "judge_goal", return_value=failure):
        assert manager.evaluate_after_turn("Worker is running", active_delegations=1)["should_continue"] is True
        assert manager.evaluate_after_turn("Worker is running", active_delegations=1)["should_continue"] is True
        assert manager.evaluate_after_turn("Worker is running")["should_continue"] is True
        assert manager.evaluate_after_turn("Worker is running")["status"] == "paused"


def test_distinct_progress_and_successful_verdict_reset_transport_repeat(goal_home):
    from hermes_cli import goals

    manager = goals.GoalManager("distinct-answer")
    manager.set("Prepare a report")
    failure = ("continue", "judge error: TimeoutError", False, None, True)
    success = ("continue", "work remains", False, None, False)
    with patch.object(goals, "judge_goal", side_effect=[failure, failure, success, failure, failure]):
        assert manager.evaluate_after_turn("Fetched source A")["should_continue"] is True
        assert manager.evaluate_after_turn("Fetched source B")["should_continue"] is True
        assert manager.evaluate_after_turn("Fetched source B")["should_continue"] is True
        assert manager.evaluate_after_turn("Fetched source B")["should_continue"] is True
        final = manager.evaluate_after_turn("Fetched source B")
    assert final["status"] == "paused"
    assert final["should_continue"] is False
