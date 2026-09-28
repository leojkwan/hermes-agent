"""A voice session never spawns the Honcho dialectic; base recall (representation + card) still injects.

The voice page opens its session with ``source="voice-safari"``, which reaches the provider as
``initialize(platform="voice-safari")``. Each walk runs init plus three substantive turns at
dialecticCadence=2 (host hermes), which covers all three spawn sites: the init prewarm, the
turn-1 dialectic wait, and the queue_prefetch cadence fire.
"""

from unittest.mock import MagicMock, patch

from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho.client import HonchoClientConfig

# Deliberately fictional test data, unrelated to user memory.
QUERY = "who is Morgan Example?"
RECALL = {"representation": "Morgan Example works with Casey Sample.", "card": "Name: Casey Sample"}


def _join_dialectic(provider) -> None:
    thread = provider._prefetch_thread
    if thread is not None:
        thread.join(timeout=10)
        assert not thread.is_alive(), "dialectic thread hung"


def _walk(platform: str):
    cfg = HonchoClientConfig(api_key="k", enabled=True, recall_mode="hybrid", dialectic_cadence=2)
    mgr = MagicMock()
    mgr.get_or_create.return_value = MagicMock(messages=[])
    mgr.get_prefetch_context.return_value = RECALL
    mgr.pop_context_result.return_value = None
    mgr.dialectic_query.return_value = "dialectic synthesis"
    provider = HonchoMemoryProvider()
    injected = []
    with patch("plugins.memory.honcho.client.HonchoClientConfig.from_global_config", return_value=cfg), \
         patch("plugins.memory.honcho.client.get_honcho_client", return_value=MagicMock()), \
         patch("plugins.memory.honcho.session.HonchoSessionManager", return_value=mgr), \
         patch("hermes_constants.get_hermes_home", return_value=MagicMock()), \
         patch.object(provider, "_spawn_dialectic", wraps=provider._spawn_dialectic) as spawn:
        provider.initialize(session_id="call", platform=platform)
        provider._init_thread.join(timeout=10)
        assert provider._session_initialized, "session init did not finish"
        _join_dialectic(provider)
        for turn in (1, 2, 3):
            provider.on_turn_start(turn, QUERY)
            injected.append(provider.prefetch(QUERY))
            provider.queue_prefetch(QUERY)
            _join_dialectic(provider)
    return [c.kwargs["thread_name"] for c in spawn.call_args_list], mgr, injected


def test_voice_session_never_spawns_dialectic_and_keeps_base_recall():
    spawned, mgr, injected = _walk("voice-safari")

    assert spawned == []
    assert mgr.dialectic_query.call_count == 0
    assert RECALL["representation"] in injected[0] and RECALL["card"] in injected[0]
    assert "dialectic synthesis" not in "".join(injected)
    assert mgr.prefetch_context.call_count == 3, "base recall must keep refreshing every turn"


def test_non_voice_session_at_same_cadence_still_spawns_dialectic():
    spawned, mgr, injected = _walk("tui")

    assert spawned == ["honcho-prewarm-dialectic", "honcho-prefetch"]
    assert mgr.dialectic_query.call_count == 2
    assert RECALL["representation"] in injected[0] and "dialectic synthesis" in injected[0]
