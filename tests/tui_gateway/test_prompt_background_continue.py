"""Optional background continuation keeps one parent's task and its stored history.

The gateway handler, JSON-RPC contract, SQLite store, and AIAgent are real.
Only the model client's HTTP call is replaced by a recording stand-in.
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
        self.requested: dict[str, threading.Event] = {}
        self.client = MagicMock()  # a Mock client keeps AIAgent on its non-streaming path
        self.client.chat.completions.create.side_effect = self._create

    def _create(self, **kwargs):
        messages = copy.deepcopy(kwargs.get("messages") or [])
        self.requests.append(messages)
        asked = next((_content(m) for m in reversed(messages) if m.get("role") == "user"), "")
        if signal := self.requested.get(asked):
            signal.set()
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


def _until_asked(gw, text):
    deadline = time.monotonic() + WAIT
    while not gw.provider.turns(text):
        assert time.monotonic() < deadline, f"{text!r} never reached the model"
        time.sleep(0.01)


def test_continue_keeps_one_background_history_in_fifo_order(gw):
    gw.provider.hold["first"] = release_first = threading.Event()
    gw.provider.hold["second"] = release_second = threading.Event()
    gw.provider.requested["third"] = third_requested = threading.Event()
    task = _wire(PARENT, "first")["result"]["task_id"]
    _until_asked(gw, "first")

    assert _wire(PARENT, "second", continue_task_id=task)["result"]["task_id"] == task
    assert _wire(PARENT, "third", continue_task_id=task)["result"]["task_id"] == task
    assert gw.built == [task]

    release_first.set()
    _until_asked(gw, "second")
    assert not third_requested.wait(2.0)  # the third model call cannot overtake the held second
    assert gw.built == [task, task]
    assert not gw.provider.turns("third")

    release_second.set()
    done = gw.wait_for(3)
    assert [payload["text"] for _sid, payload in done] == ["re: first", "re: second", "re: third"]
    assert {payload["task_id"] for _sid, payload in done} == {task}
    assert gw.bg_rows() == [task]
    assert _pairs(gw.provider.turns("third")[0]) == [
        ("user", "first"), ("assistant", "re: first"),
        ("user", "second"), ("assistant", "re: second"),
        ("user", "third")]
    assert _pairs(gw.db.get_messages_as_conversation(task))[-2:] == [
        ("user", "third"), ("assistant", "re: third")]


def test_refusals_do_no_work_and_valid_parent_follows_compression_tip(gw):
    task = _wire(PARENT, "seed")["result"]["task_id"]
    gw.wait_for(1)
    before = (list(gw.built), len(gw.provider.requests), gw.bg_rows())

    for sid, bad, code in (
        (PARENT, "bg_000000", 4040),
        (PARENT, "btw_abc123", 4004),
        (OTHER, task, 4040),
    ):
        response = _wire(sid, "should refuse", continue_task_id=bad)
        assert response["error"]["code"] == code, response

    assert (list(gw.built), len(gw.provider.requests), gw.bg_rows()) == before

    child = f"{task}-c"
    gw.db.end_session(task, "compression")
    gw.db.create_session(child, source="tui", model="test/model", parent_session_id=task)
    gw.db.append_message(child, "user", "summary")
    gw.db.append_message(child, "assistant", "re: summary")
    assert _wire(PARENT, "still mine", continue_task_id=task)["result"]["task_id"] == task
    gw.wait_for(2)
    assert _pairs(gw.provider.turns("still mine")[0]) == [
        ("user", "summary"), ("assistant", "re: summary"), ("user", "still mine")]
