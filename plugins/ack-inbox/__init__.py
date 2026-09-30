"""A profile-scoped inbox whose closure requires an explicit caller assertion.

The voice caller admits each ask before starting work. Scope is its retained
domain key, not proof of native chat ownership or human acknowledgement.
"""

from datetime import datetime, timezone
import json
import logging
import re
import sqlite3

from hermes_cli.sqlite_util import transaction
from plugins.plugin_storage import plugin_db


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIELDS = {
    "open": {"op", "scope", "request_id", "ask"},
    "list": {"op", "scope", "cursor", "limit"},
    "answered": {"op", "scope", "request_id", "answer"},
    "offer": {"op", "scope", "request_id"},
    "ack": {"op", "scope", "request_id", "explicit", "evidence"},
}
_SCHEMA = """CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    request_id TEXT NOT NULL,
    ask TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answer TEXT,
    answered_at TEXT,
    offered_at TEXT,
    acknowledged_at TEXT,
    ack_evidence TEXT,
    UNIQUE(scope, request_id)
)"""
_LOGGER = logging.getLogger(__name__)


def _text(value, maximum):
    return isinstance(value, str) and len(value.encode("utf-8")) <= maximum


def _valid(payload):
    if not isinstance(payload, dict):
        return False
    op = payload.get("op")
    if not isinstance(op, str) or op not in _FIELDS or payload.keys() - _FIELDS[op]:
        return False
    scope = payload.get("scope")
    if not isinstance(scope, str) or not _IDENTIFIER.fullmatch(scope):
        return False
    if op == "list":
        cursor, limit = payload.get("cursor", 0), payload.get("limit", 100)
        return type(cursor) is int and cursor >= 0 and type(limit) is int and 1 <= limit <= 100
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
        return False
    if op == "open":
        return _text(payload.get("ask"), 8192) and bool(payload["ask"].strip())
    if op == "answered":
        return _text(payload.get("answer"), 16384)
    if op == "ack":
        return (payload.get("explicit") is True and _text(payload.get("evidence"), 1024)
                and bool(payload["evidence"].strip()))
    return True


def _item(row):
    item = dict(row)
    item["state"] = ("acknowledged" if item["acknowledged_at"] is not None else
                     "answered" if item["answered_at"] is not None else "open")
    return item


def _operate(conn, payload):
    op, scope = payload["op"], payload["scope"]
    if op == "list":
        cursor, limit = payload.get("cursor", 0), payload.get("limit", 100)
        rows = ([] if cursor > 9223372036854775807 else conn.execute(
            "SELECT * FROM items WHERE scope=? AND acknowledged_at IS NULL AND id>? ORDER BY id LIMIT ?",
            (scope, cursor, limit + 1)).fetchall())
        items = [_item(row) for row in rows[:limit]]
        return {"ok": True, "items": items, "next_cursor": items[-1]["id"] if items else cursor,
                "more": len(rows) > limit}

    key = (scope, payload["request_id"])
    row = conn.execute("SELECT * FROM items WHERE scope=? AND request_id=?", key).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if op == "open":
        if row is not None and row["ask"] != payload["ask"]:
            return {"ok": False, "error": "conflict"}
        if row is None:
            conn.execute("INSERT INTO items(scope,request_id,ask,created_at) VALUES(?,?,?,?)",
                         (*key, payload["ask"], now))
    elif row is None:
        return {"ok": False, "error": "not_found"}
    elif op == "answered":
        if row["answer"] is not None:
            if row["answer"] != payload["answer"]:
                return {"ok": False, "error": "conflict"}
        elif row["acknowledged_at"] is not None:
            return {"ok": False, "error": "closed"}
        else:
            conn.execute("UPDATE items SET answer=?,answered_at=? WHERE scope=? AND request_id=?",
                         (payload["answer"], now, *key))
    elif op == "offer":
        if row["offered_at"] is None and row["acknowledged_at"] is None:
            conn.execute("UPDATE items SET offered_at=? WHERE scope=? AND request_id=?", (now, *key))
    elif op == "ack" and row["acknowledged_at"] is None:
        conn.execute("UPDATE items SET acknowledged_at=?,ack_evidence=? WHERE scope=? AND request_id=?",
                     (now, payload["evidence"], *key))
    row = conn.execute("SELECT * FROM items WHERE scope=? AND request_id=?", key).fetchone()
    return {"ok": True, "item": _item(row)}


def _handle(payload):
    if not _valid(payload):
        return {"ok": False, "error": "invalid_payload"}
    # Resolve the home for each operation; a loaded plugin may serve several
    # profiles, and no client field is allowed to select a storage path.
    with transaction(plugin_db("ack-inbox", "inbox.db"), immediate=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        return _operate(conn, payload)


def command(raw_args):
    try:
        if not isinstance(raw_args, str) or len(raw_args.encode("utf-8")) > 32768:
            result = {"ok": False, "error": "invalid_payload"}
        else:
            result = _handle(json.loads(raw_args))
    except (ValueError, UnicodeError):
        result = {"ok": False, "error": "invalid_payload"}
    except Exception:
        _LOGGER.exception("Acknowledgement inbox operation failed")
        result = {"ok": False, "error": "storage_error"}
    return json.dumps(result, ensure_ascii=True)


def inbox_ack(args, **kwargs):
    # Reject extra keys instead of hiding caller path/profile selection.
    if not isinstance(args, dict) or args.keys() - {"scope", "request_id", "explicit", "evidence"}:
        return json.dumps({"ok": False, "error": "invalid_payload"})
    return command(json.dumps({"op": "ack", **args}, ensure_ascii=True))


def register(ctx):
    ctx.register_command("ack-inbox", command, description="Admit, list or acknowledge inbox asks",
                         args_hint="<JSON>", argument_mode="text")
    ctx.register_tool(
        name="inbox_ack", toolset="ack_inbox", handler=inbox_ack,
        schema={"name": "inbox_ack", "description": "Record an explicit acknowledgement of one exact inbox ask. Caller evidence is an assertion; this tool does not verify human speech.",
                "parameters": {"type": "object", "properties": {
                    "scope": {"type": "string", "maxLength": 128},
                    "request_id": {"type": "string", "maxLength": 128},
                    "explicit": {"type": "boolean", "enum": [True]},
                    "evidence": {"type": "string", "maxLength": 1024}},
                    "required": ["scope", "request_id", "explicit", "evidence"],
                    "additionalProperties": False}})
