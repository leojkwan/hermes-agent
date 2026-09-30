"""Every admitted ask survives until an explicit, exact-target acknowledgement.

Exercise the installed-style plugin through native discovery and command.dispatch;
no agent turn is needed, including before a voice call has a native chat.
"""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import plugins
from hermes_cli.config import save_config
from agent.secret_scope import set_multiplex_active
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tui_gateway import server
from tools.registry import registry


def _profile(root):
    root.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "plugins" / "ack-inbox"
    # The same scenario on the unimplemented base must reach native 4018,
    # rather than fail collection or fail copying a nonexistent plugin.
    if source.exists():
        shutil.copytree(source, root / "plugins" / "ack-inbox",
                        ignore=shutil.ignore_patterns("__pycache__"))
    token = set_hermes_home_override(str(root))
    try:
        save_config({"plugins": {"enabled": ["ack-inbox"]}})
    finally:
        reset_hermes_home_override(token)
    return root


def _rpc(payload, **params):
    params = {"name": "ack-inbox", "arg": json.dumps(payload), **params}
    response = server.handle_request({"id": "inbox-test", "method": "command.dispatch",
                                      "params": params})
    assert "error" not in response, response
    assert response["result"]["type"] == "plugin", response
    return json.loads(response["result"]["output"])


def _ok(payload, **params):
    response = _rpc(payload, **params)
    assert response["ok"] is True, response
    return response


def _open(request_id, ask="Find the answer", scope="voice:call-1"):
    return {"op": "open", "scope": scope, "request_id": request_id, "ask": ask}


def _fresh_process_list(home):
    script = """
import json
from tui_gateway import server
def no_turn(*args, **kwargs):
    raise AssertionError('inbox command started a model turn')
for method in ('prompt.submit', 'prompt.background', 'session.create'):
    server._methods[method] = no_turn
response = server.handle_request({'id': 'reconstructed', 'method': 'command.dispatch',
    'params': {'name': 'ack-inbox', 'arg': json.dumps({'op': 'list', 'scope': 'voice:call-1'})}})
assert 'error' not in response, response
assert response['result']['type'] == 'plugin', response
print(response['result']['output'], file=server._real_stdout)
"""
    environment = {**os.environ, "HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.run([sys.executable, "-c", script], env=environment,
                             cwd=Path(__file__).resolve().parents[2],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout.strip().splitlines()[-1])


@pytest.fixture
def inbox_profiles(tmp_path, monkeypatch):
    a, b = (_profile(tmp_path / name) for name in ("a", "b"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(a))
    set_multiplex_active(True)
    calls = []

    def no_turn(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("inbox command must not start a model turn")

    monkeypatch.setitem(server._methods, "prompt.submit", no_turn)
    monkeypatch.setitem(server._methods, "prompt.background", no_turn)
    monkeypatch.setitem(server._methods, "session.create", no_turn)
    plugins.publish_tui_message_host(object(), no_turn)
    try:
        yield a, b, calls
    finally:
        plugins._reset_plugin_managers_for_tests()
        set_multiplex_active(False)


def test_native_inbox_retains_unfinished_and_answered_until_exact_ack(inbox_profiles):
    a, _, calls = inbox_profiles
    with server._session_profile_runtime_scope({"profile_home": str(a)}):
        original = _ok(_open("answer-1"))["item"]
        pending = _ok(_open("pending-1", "Start unfinished work"))["item"]
        manager = plugins.get_plugin_manager()
        assert manager._plugins["ack-inbox"].error is None
        assert registry.get_entry("inbox_ack", scope=manager.scope_key).toolset == "ack_inbox"
        assert _ok(_open("answer-1"))["item"] == original
        assert _rpc(_open("answer-1", "Replace the ask"))["error"] == "conflict"

        target = {"scope": "voice:call-1", "request_id": "answer-1"}
        answered = _ok({"op": "answered", **target, "answer": "The exact final answer"})["item"]
        assert answered["state"] == "answered"
        assert answered["acknowledged_at"] is None
        assert _ok({"op": "answered", **target, "answer": answered["answer"]})["item"] == answered
        assert _rpc({"op": "answered", **target, "answer": "Different"})["error"] == "conflict"
        offered = _ok({"op": "offer", **target})["item"]
        assert offered["state"] == "answered"
        assert offered["acknowledged_at"] is None
        assert _ok({"op": "offer", **target})["item"] == offered
        assert _fresh_process_list(a)["items"] == [offered, pending]

        plugins._reset_plugin_managers_for_tests()
        reopened = _ok({"op": "list", "scope": "voice:call-1"})["items"]
        assert reopened == [offered, pending]
        assert _ok(_open("answer-1"))["item"] == offered
        assert _rpc({"op": "ack", **target, "explicit": False, "evidence": "filler"})["ok"] is False
        assert _rpc({"op": "ack", **target, "explicit": True, "evidence": " "})["ok"] is False
        assert _rpc({"op": "ack", **target, "request_id": "missing",
                     "explicit": True, "evidence": "yes"})["error"] == "not_found"
        assert _rpc({"op": "ack", **target, "scope": "other-call",
                     "explicit": True, "evidence": "yes"})["error"] == "not_found"

        manager = plugins.get_plugin_manager()
        ack = json.loads(registry.dispatch("inbox_ack", {**target, "explicit": True,
                                                       "evidence": "Caller reports: understood"},
                                           scope=manager.scope_key))
        assert ack["ok"] is True
        closed = ack["item"]
        assert closed["state"] == "acknowledged"
        assert _ok({"op": "ack", **target, "explicit": True,
                    "evidence": "Retry must preserve evidence"})["item"] == closed
        assert _ok(_open("answer-1"))["item"] == closed
        assert _ok({"op": "answered", **target, "answer": "The exact final answer"})["item"] == closed
        assert _rpc({"op": "answered", **target, "answer": "late change"})["ok"] is False
        assert _ok({"op": "offer", **target})["item"] == closed
        assert _ok({"op": "list", "scope": "voice:call-1"})["items"] == [pending]
        assert _fresh_process_list(a)["items"] == [pending]

        _ok(_open("closed-pending"))
        closed_pending = {"scope": "voice:call-1", "request_id": "closed-pending"}
        acknowledged = _ok({"op": "ack", **closed_pending, "explicit": True,
                             "evidence": "Caller withdrew unfinished ask"})["item"]
        assert _rpc({"op": "answered", **closed_pending, "answer": "Late answer"})["error"] == "closed"
        assert _ok(_open("closed-pending"))["item"] == acknowledged

        # Real simultaneous callers inherit the same native profile scope.
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(copy_context().run, _ok, _open("concurrent")) for _ in range(8)]
            admitted = [future.result()["item"] for future in futures]
        assert all(item == admitted[0] for item in admitted)
        for n in range(6):
            _ok(_open(f"page-{n}"))
        cursor, all_items = 0, []
        while True:
            page = _ok({"op": "list", "scope": "voice:call-1", "cursor": cursor, "limit": 2})
            all_items.extend(page["items"])
            assert page["next_cursor"] >= cursor
            cursor = page["next_cursor"]
            if not page["more"]:
                break
        assert [item["request_id"] for item in all_items] == ["pending-1", "concurrent", *[f"page-{n}" for n in range(6)]]
        assert len({item["id"] for item in all_items}) == len(all_items)
        plugins._reset_plugin_managers_for_tests()
        assert _ok({"op": "list", "scope": "voice:call-1"})["items"] == all_items
        assert calls == []


def test_native_profile_scopes_and_invalid_calls_cannot_retarget_or_mutate(inbox_profiles):
    a, b, calls = inbox_profiles
    environment = dict(os.environ)
    with server._session_profile_runtime_scope({"profile_home": str(a)}):
        first = _ok(_open("same-id", "Profile A"))["item"]
        # Current native sessionless dispatcher ignores params.profile. Do not
        # advertise it as a route to a secondary profile or patch core here.
        _ok(_open("early", "Early local ask"), profile="b")
    with server._session_profile_runtime_scope({"profile_home": str(b)}):
        assert get_hermes_home() == b
        assert _ok({"op": "list", "scope": "voice:call-1"})["items"] == []
        second = _ok(_open("same-id", "Profile B"))["item"]
        assert second["ask"] != first["ask"]
    with server._session_profile_runtime_scope({"profile_home": str(a)}):
        before = _ok({"op": "list", "scope": "voice:call-1"})["items"]
        assert [item["ask"] for item in before] == ["Profile A", "Early local ask"]
        bad = [[], None, {}, {**_open("bad"), "home": str(b)},
               {**_open("bad"), "path": "../outside.db"}, {**_open("bad"), "profile": "b"},
               {**_open("bad"), "extra": True}, {**_open("bad"), "scope": "../escape"},
               {**_open("bad"), "request_id": "é"}, {**_open("bad"), "request_id": "x" * 129},
               {**_open("bad"), "ask": " "}, {**_open("bad"), "ask": "x" * 8193},
               {**_open("bad"), "ask": "😀" * 2049},
               {"op": "list", "scope": "voice:call-1", "cursor": -1},
               {"op": "list", "scope": "voice:call-1", "limit": True},
               {"op": "list", "scope": "voice:call-1", "limit": 101},
               {"op": "answered", "scope": "voice:call-1", "request_id": "same-id", "answer": "x" * 16385},
               {"op": "answered", "scope": "voice:call-1", "request_id": "same-id", "answer": "😀" * 4097},
               {"op": "ack", "scope": "voice:call-1", "request_id": "same-id", "explicit": 1, "evidence": "yes"},
               {"op": "ack", "scope": "voice:call-1", "request_id": "same-id", "explicit": True, "evidence": "x" * 1025},
               {"op": "ack", "scope": "voice:call-1", "request_id": "same-id", "explicit": True, "evidence": "😀" * 257}]
        for payload in bad:
            assert _rpc(payload) == {"ok": False, "error": "invalid_payload"}
        handler = plugins.get_plugin_command_handler("ack-inbox")
        oversized_raw = " " * 25000 + json.dumps(_open("raw-byte-limit", "😀" * 2000), ensure_ascii=False)
        assert len(oversized_raw) < 32768 < len(oversized_raw.encode("utf-8"))
        for raw in ("{", "x" * 32769, oversized_raw):
            assert json.loads(handler(raw)) == {"ok": False, "error": "invalid_payload"}
        assert json.loads(registry.dispatch("inbox_ack", {"scope": "voice:call-1", "request_id": "same-id",
                                                          "explicit": True, "evidence": "yes", "home": str(b)},
                                             scope=plugins.get_plugin_manager().scope_key)) == {
                                                 "ok": False, "error": "invalid_payload"}
        assert _ok({"op": "list", "scope": "voice:call-1"})["items"] == before
    assert dict(os.environ) == environment
    assert calls == []
    for home in (a, b):
        db = home / "plugin-data" / "ack-inbox" / "inbox.db"
        with sqlite3.connect(db) as conn:
            assert [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["items"]
