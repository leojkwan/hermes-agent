"""Gateway goal continuation durability and failure recovery regressions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run_goals import GatewayGoalsMixin
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_busy import GatewayBusySessionMixin
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from gateway.run_idle_gates import profile_has_pending_goal


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


class _RecoveryAdapter(BasePlatformAdapter):
    """Real BasePlatformAdapter lifecycle with a small in-memory transport."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.SLACK)
        self.sent = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)

        class Result:
            success = True
            message_id = "recovery-message"

        return Result()

    async def send_typing(self, chat_id, metadata=None):
        pass


def _recovery_source() -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK, user_id="U-recovery", chat_id="C-recovery",
        user_name="tester", chat_type="channel", thread_id="recovery-thread",
    )


class _RecoveryRunner(GatewayGoalsMixin, GatewayBusySessionMixin):
    pass


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


@pytest.mark.asyncio
async def test_fresh_runner_restores_durable_goal_into_real_adapter_and_refuses_stale_or_paused(
    isolated_goal_home, monkeypatch,
):
    """A reconstructed runner scans durable intent and dispatches through the adapter."""
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    session_id = "gateway-startup-recovery"
    source = _recovery_source()
    session_key = build_session_key(source)
    manager = GoalManager(session_id)
    manager.set("finish the durable task")
    intent = manager.prepare_continuation(
        "[stored recovery prompt] use the durable record", due_at=0,
    )
    assert intent and intent["prompt"].startswith("[stored recovery prompt]")

    # Reconstruct cached state while retaining the SQLite profile on disk.
    goals._DB_CACHE.clear()
    assert profile_has_pending_goal(isolated_goal_home)
    fresh = GoalManager(session_id)
    assert fresh.get_pending_continuation()["id"] == intent["id"]

    adapter = _RecoveryAdapter()
    handled = []

    async def handler(event):
        event._gateway_resolved_session_id = session_id
        admitted = await _MarkerRunner(_MarkerStore())._mark_durable_active_turn(event, session_key)
        if not admitted:
            return "stale intent refused"
        handled.append((event.text, event.metadata["goal_continuation_intent_id"]))
        return "recovered reply"

    adapter.set_message_handler(handler)
    runner = object.__new__(_RecoveryRunner)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._session_key_for_source = lambda _source: session_key
    runner._is_session_running = lambda _key: False
    runner._queue_depth = lambda _key, adapter=None: 0
    entry = SimpleNamespace(
        session_id=session_id, suspended=False, active_turn_token=None, origin=source,
    )

    runner._running = True
    runner._warm_goals_session_db = AsyncMock()
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn: fn())
    runner._restored_source = lambda _entry: source
    runner.session_store = SimpleNamespace(list_sessions=lambda: [entry])
    runner._adapters_for_profile = lambda _profile: {Platform.SLACK: adapter}

    from contextlib import asynccontextmanager
    def _one_scope(_runner):
        return [("recovery", isolated_goal_home)]
    monkeypatch.setattr("gateway.run._handoff_watch_scopes", _one_scope)
    @asynccontextmanager
    async def _no_profile_scope():
        yield
    monkeypatch.setattr(
        "tui_gateway.launch_profile_policy.async_launch_profile_scope_if_multiplexed",
        _no_profile_scope,
    )
    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", lambda _home: _no_profile_scope())
    sleep_calls = 0
    real_sleep = asyncio.sleep
    async def _stop_after_scan(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            runner._running = False
        await real_sleep(0)
    monkeypatch.setattr("gateway.run_goals.asyncio.sleep", _stop_after_scan)
    await asyncio.wait_for(runner._loop_wakeup_watcher(interval=0), timeout=5)
    monkeypatch.setattr("gateway.run_goals.asyncio.sleep", real_sleep)
    pending_event = adapter._pending_messages[session_key]
    assert pending_event.text == intent["prompt"]
    assert pending_event.metadata["goal_continuation_intent_id"] == intent["id"]
    adapter._pending_messages.pop(session_key)
    await adapter._process_message_background(pending_event, session_key)
    assert handled == [(intent["prompt"], intent["id"])]
    for _ in range(20):
        if session_key not in adapter._active_sessions:
            break
        await real_sleep(0)

    # A claimed old intent is surfaced as recoverable only to the marker boundary; the native
    # claim CAS refuses the duplicate and therefore cannot reach model work.
    replacement = GoalManager(session_id).prepare_continuation("second stored prompt", due_at=0)
    stale = GoalManager(session_id).claim_continuation(replacement["id"])
    assert stale is not None
    stale_adapter = _RecoveryAdapter()
    runner._delivery_adapter_for = lambda _source: stale_adapter
    await runner._goal_wakeup_fire_one(entry, source)
    duplicate = stale_adapter._pending_messages[session_key]
    duplicate._gateway_resolved_session_id = session_id
    assert not await _MarkerRunner(_MarkerStore())._mark_durable_active_turn(
        duplicate, session_key,
    )
    stale_adapter._pending_messages.pop(session_key, None)

    GoalManager(session_id).pause("operator paused")
    assert not profile_has_pending_goal(isolated_goal_home)
    await runner._goal_wakeup_fire_one(entry, source)
    assert session_key not in stale_adapter._pending_messages
