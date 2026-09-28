"""Regression: /goal continuations enqueued via the adapter FIFO are drained
automatically — no extra user message required (#47699).

Issue #47699 reported that a Slack ``/goal`` continuation could be enqueued by
``_post_turn_goal_continuation`` → ``_enqueue_fifo`` (adapter pending slot) but
never consumed until the next real inbound message woke the session.

On the current tree the continuation is enqueued while the adapter's
``_process_message_background`` frame is still live (the runner hook runs
inside ``self._message_handler(event)``), so the adapter's in-band pending
drain — and, for late arrivals, the finally-block late-arrival drain — spawns
the follow-up turn without user input.  These tests pin that contract from the
adapter dispatch layer down, so a future refactor that moves the goal hook
after the drain (or changes FIFO key derivation) fails loudly instead of
silently stalling goal loops on messaging gateways.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


class _DrainProbeAdapter(BasePlatformAdapter):
    """Minimal concrete adapter that records deliveries."""

    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.SLACK)
        self.sent: list[str] = []

    async def start(self):  # pragma: no cover - unused
        pass

    async def stop(self):  # pragma: no cover - unused
        pass

    async def connect(self):  # pragma: no cover - unused
        pass

    async def disconnect(self):  # pragma: no cover - unused
        pass

    async def get_chat_info(self, chat_id):  # pragma: no cover - unused
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)

        class _R:
            success = True
            message_id = "m1"

        return _R()

    async def send_typing(self, chat_id, metadata=None):
        pass


def _slack_thread_source() -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK,
        user_id="U1",
        chat_id="C1",
        user_name="tester",
        chat_type="channel",
        thread_id="1718600000.000100",
    )


CONTINUATION_TEXT = "[Continuing toward your standing goal]\nGoal: ship it"


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    # Pre-warm the SessionDB cache from this sync (non-loop) context so the
    # async tests' GoalManager.set() never races the bounded loop-thread
    # bootstrap window on loaded CI runners (goal silently not persisted →
    # continuation never enqueued; flaked on main run 33455779041).
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_fifo_enqueued_continuation_is_drained_without_new_user_message():
    """A continuation placed in the adapter pending slot during the handler
    frame (exactly what _post_turn_goal_continuation does via _enqueue_fifo)
    must start a second turn automatically."""
    adapter = _DrainProbeAdapter()
    src = _slack_thread_source()
    key = build_session_key(src)
    handled: list[str] = []

    async def handler(event):
        handled.append(event.text)
        if len(handled) == 1:
            # Mirror the runner's goal hook: enqueue the synthetic
            # continuation into the adapter FIFO while this frame is live.
            cont = MessageEvent(
                text=CONTINUATION_TEXT,
                message_type=MessageType.TEXT,
                source=src,
            )
            adapter._pending_messages[key] = cont
        return f"reply-{len(handled)}"

    adapter.set_message_handler(handler)
    event = MessageEvent(text="do the thing", message_type=MessageType.TEXT, source=src)

    await adapter._process_message_background(event, key)
    # The in-band drain hands off to a fresh task (#17758); let it run.
    for _ in range(40):
        # Wait for the SENDS, not just the handler calls: the delivery
        # ledger hops to worker threads around each send, so the handler
        # can return while reply-2's send is still in flight.
        if len(adapter.sent) >= 2:
            break
        await asyncio.sleep(0.05)

    assert handled == ["do the thing", CONTINUATION_TEXT], (
        "goal continuation was enqueued but never drained — "
        "a user nudge would be required (#47699)"
    )
    assert adapter.sent == ["reply-1", "reply-2"]
    # The drain task must have released the session guard when the chain ended.
    for _ in range(40):
        if key not in adapter._active_sessions:
            break
        await asyncio.sleep(0.05)
    assert key not in adapter._active_sessions


@pytest.mark.asyncio
async def test_runner_goal_hook_enqueues_into_the_key_the_adapter_drains(hermes_home):
    """_post_turn_goal_continuation resolves the FIFO key via
    _session_key_for_source; the adapter drain uses build_session_key on the
    event source. These must agree or the continuation is orphaned under a
    key nobody drains (the silent-stall shape from #47699)."""
    from unittest.mock import AsyncMock, patch
    from types import SimpleNamespace
    import uuid

    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_goals import GatewayGoalsMixin
    from hermes_cli.goals import GoalManager

    src = _slack_thread_source()
    adapter_key = build_session_key(src)

    adapter = _DrainProbeAdapter()

    class _GoalRunner(GatewayGoalsMixin, GatewayBusySessionMixin):
        _goal_max_turns_from_config = staticmethod(lambda: 10)
        _warm_goals_session_db = AsyncMock()
        _run_in_executor_with_context = AsyncMock(side_effect=lambda fn, *args: fn(*args))
        _defer_goal_status_notice_after_delivery = AsyncMock()
        _session_key_for_source = staticmethod(lambda _source: adapter_key)
        _delivery_adapter_for = staticmethod(lambda _source: adapter)

    runner = _GoalRunner()
    session_entry = SimpleNamespace(session_id=f"goal-sess-{uuid.uuid4().hex[:8]}")

    GoalManager(session_entry.session_id).set("ship it")
    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "still needs work", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="partial progress",
        )
        await asyncio.sleep(0.05)

    assert adapter_key in adapter._pending_messages, (
        "continuation enqueued under a different key than the adapter "
        f"drains: pending keys={list(adapter._pending_messages)} "
        f"expected={adapter_key}"
    )


@pytest.mark.asyncio
async def test_due_goal_wakeup_starts_an_idle_adapter_turn(hermes_home):
    """A saved due goal must enter the real adapter while its session is idle."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_goals import GatewayGoalsMixin
    from gateway.run_inbound import GatewayInboundMixin
    from hermes_cli.goals import GoalManager

    source = _slack_thread_source()
    key = build_session_key(source)
    goal = GoalManager("idle-goal-wakeup")
    goal.set("ship it")
    pending = goal.get_pending_continuation()
    assert pending is not None

    adapter = _DrainProbeAdapter()
    handled: list[str] = []

    async def handler(event):
        event._gateway_resolved_session_id = "idle-goal-wakeup"
        assert await runner._mark_durable_active_turn(event, key)
        handled.append(event.text)
        assert await runner._clear_durable_active_turn(event)
        return "continued"

    adapter.set_message_handler(handler)

    class _WakeRunner(GatewayGoalsMixin, GatewayBusySessionMixin, GatewayInboundMixin):
        _session_key_for_source = staticmethod(lambda _source: key)
        _delivery_adapter_for = staticmethod(lambda _source: adapter)
        _is_session_running = staticmethod(lambda _key: False)
        _peek_session_state = staticmethod(lambda _key: None)

    runner = _WakeRunner()
    runner.async_session_store = SimpleNamespace(
        mark_turn_active=AsyncMock(return_value="turn-token"),
        clear_turn_active=AsyncMock(return_value=True),
    )

    await runner._goal_wakeup_fire_one(
        SimpleNamespace(session_id="idle-goal-wakeup", active_turn_token=None), source,
    )
    for _ in range(40):
        if adapter.sent:
            break
        await asyncio.sleep(0.05)

    assert handled == [pending["prompt"]]
    assert adapter.sent == ["continued"]
    assert key not in adapter._pending_messages
    assert goal.get_pending_continuation() is None
    for _ in range(40):
        if key not in adapter._active_sessions:
            break
        await asyncio.sleep(0.05)
    await runner._goal_wakeup_fire_one(
        SimpleNamespace(session_id="idle-goal-wakeup", active_turn_token=None), source,
    )
    assert handled == [pending["prompt"]]
