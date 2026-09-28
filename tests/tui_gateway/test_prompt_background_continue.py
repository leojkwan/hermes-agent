"""``prompt.background`` ``continue_task_id``: an ask continues an earlier background session.

Voice calls fan every overlapping ask out to ``prompt.background``, which minted a fresh ``bg_*``
session per ask: one call left 42 disconnected background chats, and asking about the same topic
twice started from zero. With ``continue_task_id`` the ask runs as the NEXT turn of that session
(same ``bg_*`` row, its durable transcript replayed to the model); an ask landing while that
session's turn still runs queues behind it (FIFO, never two turns at once); an unknown, foreign or
malformed id is refused before any work starts.

The handler, ``_spawn_side_agent``, its threads, the event contract check and the SQLite store are
real; so is ``AIAgent`` — only the provider's HTTP client is a stand-in that records every request.
"""

from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import hermes_state_registry as registry
import run_agent
from tui_gateway import server

PARENT, OTHER = "ui-parent", "ui-other"
WAIT = 60.0


def _content(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


class _Provider:
    """The provider's HTTP client. Answers ``re: <last user text>``; ``hold`` keeps a request open."""

    def __init__(self):
        self.requests: list[list[dict]] = []
        self.hold: dict[str, threading.Event] = {}
        self.client = MagicMock()  # a Mock client keeps AIAgent on its non-streaming path
        self.client.chat.completions.create.side_effect = self._create

    def _create(self, **kwargs):
        messages = copy.deepcopy(kwargs.get("messages") or [])
        self.requests.append(messages)
        asked = next((_content(m) for m in reversed(messages) if m.get("role") == "user"), "")
        if (gate := self.hold.get(asked)) is not None:
            assert gate.wait(WAIT), f"request for {asked!r} was never released"
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=f"re: {asked}", tool_calls=None), finish_reason="stop")],
            model="test/model", usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5))

    def turns(self, asked: str) -> list[list[dict]]:
        """Every request whose newest user message is ``asked``."""
        return [r for r in self.requests
                if next((_content(m) for m in reversed(r) if m.get("role") == "user"), None) == asked]


def _pairs(messages: list[dict]) -> list[tuple[str, str]]:
    return [(m["role"], _content(m)) for m in messages if m.get("role") in ("user", "assistant")]


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """Two live gateway sessions sharing one real store; every emitted frame recorded."""
    db = registry.acquire(tmp_path / "state.db")
    frames: list[dict] = []
    arrived = threading.Condition()

    def write_json(frame):
        with arrived:
            frames.append(frame)
            arrived.notify_all()
        return True

    monkeypatch.setattr(server, "write_json", write_json)
    provider = _Provider()
    built: list[str] = []

    class RecordingAgent(run_agent.AIAgent):
        def __init__(self, **kwargs):
            built.append(kwargs["session_id"])
            super().__init__(**kwargs)

    def live(key):
        parent_agent = SimpleNamespace(
            model="test/model", provider="openrouter", base_url="https://openrouter.ai/api/v1",
            api_key="test-key", api_mode="chat_completions", _fallback_chain=[], _session_db=db)
        return {"agent": parent_agent, "session_key": key, "profile_home": None, "cwd": str(tmp_path),
                "history": [], "history_lock": threading.RLock(), "running": False}

    with server._sessions_lock:
        saved = dict(server._sessions)
        server._sessions.update({PARENT: live("parent-key"), OTHER: live("other-key")})
    try:
        with patch("run_agent.AIAgent", RecordingAgent), \
                patch("agent.process_bootstrap.OpenAI", return_value=provider.client), \
                patch("model_tools.get_tool_definitions", return_value=[]), \
                patch("model_tools.check_toolset_requirements", return_value={}), \
                patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
                patch("agent.title_generator.maybe_auto_title", lambda *a, **k: None):
            env = SimpleNamespace(db=db, provider=provider, built=built)

            def completions():
                return [(f["params"]["session_id"], f["params"]["payload"]) for f in list(frames)
                        if (f.get("params") or {}).get("type") == "background.complete"]

            def wait_for(n):
                with arrived:
                    assert arrived.wait_for(lambda: len(completions()) >= n, timeout=WAIT), completions()
                return completions()

            def bg_rows():
                return sorted(r["id"] for r in db.search_sessions(limit=1000) if r["id"].startswith("bg_"))

            env.completions, env.wait_for, env.bg_rows = completions, wait_for, bg_rows
            try:
                yield env
            finally:
                for gate in provider.hold.values():  # a failed assertion must not strand a held turn
                    gate.set()
    finally:
        with server._sessions_lock:
            server._sessions.clear()
            server._sessions.update(saved)
        registry.close_all()


def _handler(sid, text, **extra):
    """The registered JSON-RPC handler, called the way ``test_launch_db_home_override_race`` does."""
    return server._methods["prompt.background"]("rid", {"session_id": sid, "text": text, **extra})


def _wire(sid, text, **extra):
    """The full JSON-RPC path: contract admission (unknown keys → 4000) + handler."""
    return server.handle_request({"jsonrpc": "2.0", "id": "rid", "method": "prompt.background",
                                  "params": {"session_id": sid, "text": text, **extra}})


def test_continue_runs_the_next_turn_of_the_same_background_session(gw):
    first = _handler(PARENT, "who is Morgan Example?")["result"]["task_id"]
    gw.wait_for(1)

    again = _handler(PARENT, "what did I just ask you about?", continue_task_id=first)

    assert again["result"]["task_id"] == first
    gw.wait_for(2)
    assert gw.bg_rows() == [first]  # one background session, not two
    assert gw.built == [first, first]
    # The model saw turn 1 when answering turn 2.
    (second_request,) = gw.provider.turns("what did I just ask you about?")
    assert _pairs(second_request) == [
        ("user", "who is Morgan Example?"), ("assistant", "re: who is Morgan Example?"),
        ("user", "what did I just ask you about?")]
    assert _pairs(gw.db.get_messages_as_conversation(first)) == [
        ("user", "who is Morgan Example?"), ("assistant", "re: who is Morgan Example?"),
        ("user", "what did I just ask you about?"), ("assistant", "re: what did I just ask you about?")]
    assert gw.completions() == [
        (PARENT, {"task_id": first, "text": "re: who is Morgan Example?"}),
        (PARENT, {"task_id": first, "text": "re: what did I just ask you about?"})]


def test_without_continue_every_ask_is_still_a_fresh_session(gw):
    a = _wire(PARENT, "alpha")["result"]["task_id"]
    b = _wire(PARENT, "beta")["result"]["task_id"]
    gw.wait_for(2)

    assert a != b and gw.bg_rows() == sorted([a, b])
    assert _pairs(gw.provider.turns("beta")[0]) == [("user", "beta")]


def _until_asked(gw, text):
    deadline = time.monotonic() + WAIT
    while not gw.provider.turns(text):
        assert time.monotonic() < deadline, f"{text!r} never reached the model"
        time.sleep(0.01)


def test_an_ask_landing_mid_turn_queues_behind_it_in_order(gw):
    """Each queued ask waits for the one before it, not merely the running one. (The agent's own
    durable turn lease would also make a racing turn wait and reload, which is why this holds
    turn 2 open too: only the gateway queue keeps turn 3 from even starting.)"""
    gw.provider.hold["first"] = release_first = threading.Event()
    gw.provider.hold["second"] = release_second = threading.Event()
    task = _wire(PARENT, "first")["result"]["task_id"]
    _until_asked(gw, "first")

    assert _wire(PARENT, "second", continue_task_id=task)["result"]["task_id"] == task
    assert _wire(PARENT, "third", continue_task_id=task)["result"]["task_id"] == task
    time.sleep(1.0)  # a turn that did not queue would have built its agent and asked the model by now
    assert gw.built == [task]
    assert not gw.provider.turns("second") and not gw.provider.turns("third")
    assert gw.completions() == []

    release_first.set()
    _until_asked(gw, "second")
    time.sleep(1.0)
    assert gw.built == [task, task]  # turn 3 is still queued behind the running turn 2
    assert [payload["text"] for _sid, payload in gw.completions()] == ["re: first"]

    release_second.set()
    done = gw.wait_for(3)

    assert [payload["text"] for _sid, payload in done] == ["re: first", "re: second", "re: third"]
    assert {payload["task_id"] for _sid, payload in done} == {task}
    assert gw.built == [task, task, task]
    (third_request,) = gw.provider.turns("third")
    assert _pairs(third_request) == [
        ("user", "first"), ("assistant", "re: first"), ("user", "second"), ("assistant", "re: second"),
        ("user", "third")]
    assert gw.bg_rows() == [task]


@pytest.mark.parametrize("bad, code", [
    ("", 4004), ("bg_", 4004), ("bg_ZZZZZZ", 4004), (" bg_abc123", 4004), ("btw_abc123", 4004),
    ("parent-key", 4004), (123, 4004), ("bg_000000", 4040)])
def test_malformed_or_unknown_ids_are_refused_without_side_effects(gw, bad, code):
    task = _wire(PARENT, "seed")["result"]["task_id"]
    gw.wait_for(1)
    assert task != "bg_000000"
    before = (list(gw.built), len(gw.provider.requests), gw.bg_rows())

    resp = _wire(PARENT, "hello", continue_task_id=bad)

    assert resp["error"]["code"] == code, resp
    time.sleep(0.2)
    assert (list(gw.built), len(gw.provider.requests), gw.bg_rows()) == before
    assert len(gw.completions()) == 1


def test_another_sessions_background_task_is_refused_and_left_intact(gw):
    task = _wire(PARENT, "mine")["result"]["task_id"]
    gw.wait_for(1)
    before = (list(gw.built), len(gw.provider.requests), gw.bg_rows())

    resp = _wire(OTHER, "let me in", continue_task_id=task)

    assert resp["error"]["code"] == 4040, resp
    time.sleep(0.2)
    assert (list(gw.built), len(gw.provider.requests), gw.bg_rows()) == before
    assert _pairs(gw.db.get_messages_as_conversation(task)) == [("user", "mine"), ("assistant", "re: mine")]
    # The refusal did not break the owner's chain: its next ask still continues the session.
    assert _wire(PARENT, "still mine?", continue_task_id=task)["result"]["task_id"] == task
    gw.wait_for(2)
    assert _pairs(gw.provider.turns("still mine?")[0])[:2] == [("user", "mine"), ("assistant", "re: mine")]


def test_continue_follows_a_compression_rotated_background_session(gw):
    """Compression ends the row and forks a child holding the compacted transcript (the lineage
    shape ``conversation_compression`` writes); the next ask continues the child, not the parent."""
    seen: list[tuple[str, list | None]] = []

    class CompressingAgent:
        def __init__(self, **kwargs):
            self.session_id, self._db = kwargs["session_id"], kwargs["session_db"]

        def run_conversation(self, *, user_message, task_id, conversation_history=None):
            seen.append((self.session_id, conversation_history))
            db, parent, child = self._db, self.session_id, f"{self.session_id}-c"
            db.create_session(parent, source="tui", model="m")
            db.append_message(parent, "user", user_message)
            db.append_message(parent, "assistant", "30k tokens of findings")
            db.end_session(parent, "compression")
            db.create_session(child, source="tui", model="m", parent_session_id=parent)
            db.append_message(child, "user", f"[summary] {user_message}")
            db.append_message(child, "assistant", f"re: {user_message}")
            self.session_id = child
            return {"final_response": f"re: {user_message}"}

    with patch("run_agent.AIAgent", CompressingAgent):
        task = _handler(PARENT, "long research")["result"]["task_id"]
        gw.wait_for(1)
        assert _handler(PARENT, "and then?", continue_task_id=task)["result"]["task_id"] == task
        gw.wait_for(2)

    assert seen[0] == (task, None)
    assert seen[1][0] == f"{task}-c"
    assert _pairs(seen[1][1]) == [("user", "[summary] long research"), ("assistant", "re: long research")]
