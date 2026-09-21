"""Workspace terminal marker: the compliant exit for tool-less kanban lanes.

A lane with no ``kanban_*`` tools bound (muse-bridge, tool-less fallback
models) cannot file a terminal board call; it writes
``$HERMES_KANBAN_WORKSPACE/kanban-terminal.json`` instead and the dispatcher's
dead-worker sweep replays it as a real complete/block. Only a fresh marker
addressed to the current run counts — stale or foreign markers read as None.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent import kanban_stop as ks


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_marker")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(ws))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:1:abc")
    return ws


def _payload(**kw):
    base = {
        "action": "complete",
        "task_id": "t_marker",
        "run_token": "host:1:abc",
        "summary": "did the thing",
        "written_at": time.time(),
    }
    base.update(kw)
    return base


def _write_marker(ws, payload):
    path = ws / ks.TERMINAL_MARKER_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_marker_roundtrip(worker_env):
    _write_marker(worker_env, _payload())
    marker = ks.read_fresh_terminal_marker()
    assert marker is not None
    assert marker["action"] == "complete"
    assert marker["summary"] == "did the thing"


def test_write_marker_helper_roundtrip(worker_env):
    path = ks.write_kanban_terminal_marker("block", reason="need input")
    assert path and path.endswith(ks.TERMINAL_MARKER_FILENAME)
    marker = ks.read_fresh_terminal_marker()
    assert marker is not None
    assert marker["action"] == "block"
    assert marker["reason"] == "need input"
    # The writer binds the run token so only this run's sweep honors it.
    assert marker["run_token"] == "host:1:abc"


def test_write_marker_rejects_bad_action(worker_env):
    assert ks.write_kanban_terminal_marker("review") is None
    assert not (worker_env / ks.TERMINAL_MARKER_FILENAME).exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": "t_other"},  # foreign task
        {"run_token": "host:1:old"},  # prior run's claim
        {"written_at": time.time() - 13 * 3600},  # stale
        {"written_at": 0},  # missing clock
        {"action": "review"},  # not a terminal action
        {"action": None},
        {"summary": "x" * 2001},  # over the text limit
        {"artifacts": "not-a-list"},
        {"extra": 1},  # unknown keys fail closed
    ],
)
def test_invalid_markers_read_as_none(worker_env, overrides):
    _write_marker(worker_env, _payload(**overrides))
    assert ks.read_fresh_terminal_marker() is None


def test_missing_marker_reads_as_none(worker_env):
    assert ks.read_fresh_terminal_marker() is None


def test_dispatcher_side_explicit_args(tmp_path):
    # The sweep passes the task row's workspace/claim lock explicitly (its own
    # env carries neither).
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_marker(ws, _payload())
    assert (
        ks.read_fresh_terminal_marker(
            "t_marker", workspace=str(ws), expected_run_token="host:1:abc"
        )
        is not None
    )
    assert (
        ks.read_fresh_terminal_marker(
            "t_marker", workspace=str(ws), expected_run_token="host:1:other"
        )
        is None
    )


def test_clear_removes_marker(worker_env):
    _write_marker(worker_env, _payload())
    assert ks.clear_kanban_terminal_marker() is True
    assert ks.read_fresh_terminal_marker() is None
    assert ks.clear_kanban_terminal_marker() is False


def test_agent_has_kanban_tools():
    assert ks.agent_has_kanban_tools(SimpleNamespace(valid_tool_names=None)) is True
    assert ks.agent_has_kanban_tools(SimpleNamespace()) is True  # fail open
    assert (
        ks.agent_has_kanban_tools(
            SimpleNamespace(valid_tool_names={"read_file", "kanban_complete"})
        )
        is True
    )
    assert (
        ks.agent_has_kanban_tools(
            SimpleNamespace(valid_tool_names={"read_file", "write_file"})
        )
        is False
    )


def test_nudge_points_toolless_lane_at_marker(worker_env):
    nudge = ks.build_kanban_stop_nudge(messages=[], lane_has_kanban_tools=False)
    assert nudge is not None
    assert ks.TERMINAL_MARKER_FILENAME in nudge
    assert "t_marker" in nudge
    # It must not demand tool calls the lane cannot make.
    assert "kanban_complete(summary" not in nudge


def test_fresh_marker_satisfies_toolless_guard(worker_env):
    _write_marker(worker_env, _payload())
    assert ks.build_kanban_stop_nudge(messages=[], lane_has_kanban_tools=False) is None


def test_classic_nudge_unchanged_for_capable_lane(worker_env):
    nudge = ks.build_kanban_stop_nudge(messages=[], lane_has_kanban_tools=True)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert ks.TERMINAL_MARKER_FILENAME not in nudge
