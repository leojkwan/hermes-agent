"""Gateway goal continuation durability and failure recovery regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run_goals import GatewayGoalsMixin
from gateway.run_inbound import GatewayInboundMixin


@pytest.fixture()
def isolated_goal_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


class _Manager:
    def __init__(self, session_id, **_kwargs):
        self.session_id = session_id
        self.decisions = []
        self.prepared = []

    def is_active(self):
        return True

    def evaluate_after_turn(self, *args, **kwargs):
        self.decisions.append((args, kwargs))
        return {
            "should_continue": True,
            "continuation_prompt": "stored continuation",
            "message": "continuing",
            "kind": "continue",
        }

    def prepare_continuation(self, prompt, **kwargs):
        self.prepared.append((prompt, kwargs))
        return {"id": "intent-1", "prompt": prompt, "kind": kwargs.get("kind", "continue")}

    def record_turn_failure(self, result):
        self.failed = result
        return {"message": "paused", "should_continue": False}


class _Adapter:
    def __init__(self):
        self.enqueued = []


class _Runner(GatewayGoalsMixin):
    def __init__(self, manager):
        self.manager = manager
        self.config = {"goals": {"max_turns": 3}}
        self.async_session_store = SimpleNamespace()
        self.async_session_store.get_or_create_session = AsyncMock(
            return_value=SimpleNamespace(session_id="sid")
        )
        self._delivery_adapter_for = lambda _source: _Adapter()
        self._session_key_for_source = lambda _source: "key"
        self._run_in_executor_with_context = AsyncMock(side_effect=lambda fn: fn())
        self._defer_goal_status_notice_after_delivery = AsyncMock()
        self._enqueue_fifo = lambda _key, event, adapter: adapter.enqueued.append((event, adapter))

    async def _post_turn_manager(self, *_args, **_kwargs):
        return self.manager


@pytest.mark.asyncio
async def test_goal_decision_is_persisted_before_fifo_enqueue(monkeypatch):
    manager = _Manager("sid")
    runner = _Runner(manager)
    source = SimpleNamespace()

    await runner._post_turn_goal_continuation(
        session_entry=SimpleNamespace(session_id="sid"),
        source=source,
        final_response="progress",
    )

    assert manager.prepared == [("stored continuation", {"kind": "continue", "reason": "", "due_at": None})]


@pytest.mark.asyncio
async def test_empty_failed_turn_is_recorded_without_running_goal_judge(monkeypatch):
    manager = _Manager("sid")
    runner = _Runner(manager)
    runner._post_turn_manager = AsyncMock(return_value=manager)
    source = SimpleNamespace()

    await runner._run_post_turn_hooks(
        agent_result={
            "failed": True, "completed": False, "failure_retryable": True,
            "failure_reason": "provider unavailable", "final_response": "",
        },
        source=source,
        is_internal=False,
        event=SimpleNamespace(),
    )

    assert manager.failed["failed"] is True
    assert manager.decisions == []


class _MarkerStore:
    async def mark_turn_active(self, _session_key):
        return "marker-token"

    async def clear_turn_active(self, _session_key, _token):
        return True


class _MarkerWriteFailure:
    async def mark_turn_active(self, _session_key):
        return None


class _MarkerRunner(GatewayInboundMixin):
    def __init__(self, store):
        self.async_session_store = store


@pytest.mark.asyncio
async def test_marker_boundary_acknowledges_intent_before_model_boundary(isolated_goal_home):
    from hermes_cli.goals import GoalManager

    manager = GoalManager("gateway-marker-ack")
    manager.set("finish the task")
    intent = manager.prepare_continuation("continue from durable state")
    event = SimpleNamespace(
        metadata={
            "goal_continuation_intent_id": intent["id"],
            "goal_continuation_session_id": "foreign-value-is-ignored",
        },
        internal=True,
    )
    runner = _MarkerRunner(_MarkerStore())
    event._gateway_resolved_session_id = "gateway-marker-ack"

    assert await runner._mark_durable_active_turn(event, "session-key")
    assert GoalManager("gateway-marker-ack").get_pending_continuation() is None


@pytest.mark.asyncio
async def test_marker_write_failure_leaves_intent_for_recovery(isolated_goal_home):
    from hermes_cli.goals import GoalManager

    manager = GoalManager("gateway-marker-write-failure")
    manager.set("finish the task")
    intent = manager.prepare_continuation("continue from durable state")
    event = SimpleNamespace(
        metadata={"goal_continuation_intent_id": intent["id"]}, internal=True,
    )
    event._gateway_resolved_session_id = "gateway-marker-write-failure"

    assert not await _MarkerRunner(_MarkerWriteFailure())._mark_durable_active_turn(event, "session-key")
    assert GoalManager("gateway-marker-write-failure").get_pending_continuation()["id"] == intent["id"]


@pytest.mark.asyncio
async def test_duplicate_queued_intent_is_skipped_without_model_boundary(isolated_goal_home):
    from hermes_cli.goals import GoalManager

    manager = GoalManager("gateway-duplicate-intent")
    manager.set("finish the task")
    intent = manager.prepare_continuation("stored prompt")
    claimed = manager.claim_continuation(intent["id"])
    event = SimpleNamespace(
        metadata={"goal_continuation_intent_id": intent["id"]}, internal=True,
    )
    event._gateway_resolved_session_id = "gateway-duplicate-intent"

    assert not await _MarkerRunner(_MarkerStore())._mark_durable_active_turn(event, "session-key")
    assert GoalManager("gateway-duplicate-intent").get_pending_continuation()["claim_token"] == claimed["claim_token"]
