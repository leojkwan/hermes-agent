"""FBL-371: Slack sender identity survives early auth and reaches the loop guards."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings
from tests.gateway.test_slack_peer_agent_smoke import (
    BOT_USER_ID,
    PEER_USER_ID,
    TEAM_ID,
    _make_event,
    smoke_adapter as peer_smoke_adapter,
)

smoke_adapter = peer_smoke_adapter


@pytest.fixture
def authorized_adapter(smoke_adapter, monkeypatch, request):
    from gateway.run import GatewayRunner

    for name in ("SLACK_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_OWNER")
    monkeypatch.setenv("SLACK_ALLOW_BOTS", "mentions")
    monkeypatch.delenv("SLACK_API_HUMAN_USERS", raising=False)
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *args, **kwargs: False)
    runner._bot_loop_guard = BotLoopGuard(settings=lambda: BotLoopGuardSettings(max_events=1))
    runner._adapter_profile_for_source = lambda source: None
    runner._under_authorization_profile = lambda source, fn: fn()
    smoke_adapter._hydrate_thread_context = AsyncMock(return_value=(None, [], []))
    smoke_adapter._collect_inbound_media = AsyncMock(return_value=([], [], [], "hello"))
    smoke_adapter._app.client.users_info.return_value = {"user": {"is_bot": True}}

    if request.param == "callback":
        def check(user_id, chat_type, chat_id, *, is_bot=False, thread_id=None):
            return runner._is_user_authorized(smoke_adapter.build_source(
                chat_id=chat_id, chat_type=chat_type, user_id=user_id,
                thread_id=thread_id, is_bot=is_bot))
        smoke_adapter.set_authorization_check(check)
    else:
        # Bound-method fallback used by non-multiplexed runners.
        smoke_adapter._message_handler = SimpleNamespace(__self__=runner)
    return smoke_adapter, runner


def _event(**overrides):
    event = _make_event(text=f"<@{BOT_USER_ID}> hello", user=PEER_USER_ID, ts="1700000000.2")
    event.update(overrides)
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
@pytest.mark.parametrize("declared", [True, False])
async def test_allowed_bot_identity_survives_auth_and_loop_guards(authorized_adapter, declared):
    adapter, runner = authorized_adapter
    event = _event(**({"bot_id": "B_PEER"} if declared else {}))
    await adapter._handle_slack_message(event)
    if declared:
        adapter._app.client.users_info.assert_not_awaited()
    else:
        adapter._app.client.users_info.assert_awaited_once_with(user=PEER_USER_ID)
    adapter.handle_message.assert_awaited_once()
    message = adapter.handle_message.await_args.args[0]
    assert message.source.is_bot is True
    assert runner._is_user_authorized(message.source) is True
    assert runner._admit_bot_chain(message) is True
    assert runner._admit_bot_chain(message) is False
    assert runner._admit_bot_message(message.source) is True
    assert runner._admit_bot_message(message.source) is False
    assert runner._is_user_authorized(message.source) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
@pytest.mark.parametrize("mode,mentioned,own", [("none", True, False), ("mentions", False, False), ("all", True, True)])
async def test_bot_filters_remain_closed(authorized_adapter, monkeypatch, mode, mentioned, own):
    adapter, _ = authorized_adapter
    monkeypatch.setenv("SLACK_ALLOW_BOTS", mode)
    event = _event(bot_id="B_PEER", user=BOT_USER_ID if own else PEER_USER_ID,
                   text=f"<@{BOT_USER_ID}> hello" if mentioned else "hello")
    await adapter._handle_slack_message(event)
    adapter.handle_message.assert_not_awaited()
    adapter._hydrate_thread_context.assert_not_awaited()
    adapter._collect_inbound_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
@pytest.mark.parametrize("lookup,event_fields,payload", [
    ("human", {}, {"user": {"is_bot": False}}),
    ("api_human", {"app_id": "A_FRONTEND"}, {"user": {"is_bot": False}}),
    ("failed", {}, None),
    ("unknown", {}, {}),
    ("forged", {"is_bot": True, "_hermes_is_bot": True, "client_msg_id": "human-message"},
     {"user": {"is_bot": False}}),
])
async def test_unverified_or_human_sender_cannot_get_bot_exemption(
        authorized_adapter, lookup, event_fields, payload):
    adapter, _ = authorized_adapter
    event = _event(**event_fields)
    adapter._app.client.users_info.return_value = payload
    adapter.config.extra["api_human_users"] = [PEER_USER_ID] if lookup == "api_human" else []
    if lookup == "failed":
        adapter._app.client.users_info.side_effect = RuntimeError("synthetic lookup failure")
    await adapter._handle_slack_message(event)
    adapter.handle_message.assert_not_awaited()
    adapter._hydrate_thread_context.assert_not_awaited()
    adapter._collect_inbound_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
async def test_outer_workspace_lookup_cannot_reuse_another_teams_bot(authorized_adapter):
    adapter, _ = authorized_adapter
    adapter._user_is_bot_cache[(TEAM_ID, PEER_USER_ID)] = True
    adapter._app.client.users_info.return_value = {"user": {"is_bot": False}}
    event = _event()
    event.pop("team")
    await adapter._handle_slack_message(event, {"team_id": "T_OTHER"})
    adapter.handle_message.assert_not_awaited()
    adapter._hydrate_thread_context.assert_not_awaited()
    assert adapter._user_is_bot_cache[("T_OTHER", PEER_USER_ID)] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
async def test_unauthorized_ordinary_human_rejected_without_lookup(authorized_adapter):
    adapter, _ = authorized_adapter
    adapter._app.client.users_info.return_value = {"user": {"is_bot": False}}
    await adapter._handle_slack_message(_event(user="U_UNAUTHORIZED", client_msg_id="human-message"))
    adapter._app.client.users_info.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    adapter._hydrate_thread_context.assert_not_awaited()
    adapter._collect_inbound_media.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
@pytest.mark.parametrize("mode,mentioned", [("none", True), ("mentions", False), ("mentions", True)])
async def test_allowlisted_client_message_bot_obeys_filters_and_loop_guards(
        authorized_adapter, monkeypatch, mode, mentioned):
    adapter, runner = authorized_adapter
    monkeypatch.setenv("SLACK_ALLOW_BOTS", mode)
    event = _event(user="U_OWNER", client_msg_id="client-message",
                   text=f"<@{BOT_USER_ID}> hello" if mentioned else "hello",
                   _hermes_force_process=True)
    await adapter._handle_slack_message(event)
    if mode == "none" or not mentioned:
        adapter.handle_message.assert_not_awaited()
        adapter._hydrate_thread_context.assert_not_awaited()
        adapter._collect_inbound_media.assert_not_awaited()
    else:
        adapter.handle_message.assert_awaited_once()
        message = adapter.handle_message.await_args.args[0]
        assert message.source.is_bot is True
        assert runner._admit_bot_chain(message) is True
        assert runner._admit_bot_chain(message) is False
        assert runner._admit_bot_message(message.source) is True
        assert runner._admit_bot_message(message.source) is False
    adapter._app.client.users_info.assert_awaited_once_with(user="U_OWNER")


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
async def test_allowed_human_stays_human(authorized_adapter):
    adapter, _ = authorized_adapter
    adapter._app.client.users_info.return_value = {"user": {"is_bot": False}}
    await adapter._handle_slack_message(_event(user="U_OWNER", client_msg_id="human-message"))
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].source.is_bot is False


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
@pytest.mark.parametrize("mode", ["mentions", "all"])
@pytest.mark.parametrize("lookup", ["human", "missing", "error", "bot"])
async def test_ambiguous_api_sender_requires_verified_bot_identity(
        authorized_adapter, monkeypatch, mode, lookup):
    adapter, runner = authorized_adapter
    monkeypatch.setenv("SLACK_ALLOW_BOTS", mode)
    adapter._app.client.users_info.return_value = (
        {} if lookup == "missing" else {"user": {"is_bot": lookup == "bot"}})
    if lookup == "error":
        adapter._app.client.users_info.side_effect = RuntimeError("synthetic lookup failure")
    await adapter._handle_slack_message(_event(user="U_UNALLOWLISTED_HUMAN", app_id="A_FRONTEND"))
    if lookup == "bot":
        adapter.handle_message.assert_awaited_once()
        source = adapter.handle_message.await_args.args[0].source
        assert source.is_bot is True
        assert runner._is_user_authorized(source) is True
    else:
        adapter.handle_message.assert_not_awaited()
        adapter._hydrate_thread_context.assert_not_awaited()
        adapter._collect_inbound_media.assert_not_awaited()
    adapter._app.client.users_info.assert_awaited_once_with(user="U_UNALLOWLISTED_HUMAN")


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized_adapter", ["callback", "fallback"], indirect=True)
@pytest.mark.parametrize("mode", ["none", "mentions", "all"])
@pytest.mark.parametrize("override", [True, False])
async def test_authorized_api_human_preserves_filter_policy_without_bot_exemption(
        authorized_adapter, monkeypatch, mode, override):
    from dataclasses import replace

    adapter, runner = authorized_adapter
    monkeypatch.setenv("SLACK_ALLOW_BOTS", mode)
    adapter.config.extra["api_human_users"] = ["U_OWNER"] if override else []
    adapter._app.client.users_info.return_value = {"user": {"is_bot": False}}
    await adapter._handle_slack_message(_event(user="U_OWNER", app_id="A_FRONTEND"))
    if mode == "none" and not override:
        adapter.handle_message.assert_not_awaited()
    else:
        adapter.handle_message.assert_awaited_once()
        source = adapter.handle_message.await_args.args[0].source
        assert source.is_bot is False
        assert runner._is_user_authorized(source) is True
        assert runner._is_user_authorized(replace(source, user_id="U_UNAUTHORIZED")) is False


def test_legacy_three_argument_callback_keeps_human_semantics(smoke_adapter):
    smoke_adapter.set_authorization_check(lambda user, chat_type, chat_id: user == "U_OWNER")
    assert smoke_adapter._early_reject_unauthorized("U_OWNER", "C_SMOKE", False) is False
    assert smoke_adapter._early_reject_unauthorized("U_OTHER", "C_SMOKE", False) is True
