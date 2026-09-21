"""Turn-end guard for kanban workers, which must end with a terminal board tool that hands
the card to whoever owns it next (``kanban_complete``, ``kanban_block``,
``kanban_request_review``, ``kanban_request_changes``). Some models narrate the next step
and stop with no tool calls; Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``. Policy-only: return a bounded synthetic nudge so the loop continues
instead of exiting.

Lanes that cannot call board tools at all (no ``kanban_*`` tool bound — e.g. muse-bridge
lanes on a sandboxed board DB) still owe the board a terminal state, so their nudge points
at the workspace terminal marker instead: ``kanban-terminal.json`` in ``$HERMES_KANBAN_WORKSPACE``,
written via plain file tools. The dispatcher's dead-worker sweep replays a fresh marker as a
real ``kanban_complete`` / ``kanban_block`` instead of a protocol violation. The marker is a
deliberately narrow carve-out — see ``write_kanban_terminal_marker`` for the exact contract.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterable, Optional

from agent.delegation_context import owned_kanban_task

_log = logging.getLogger("agent.kanban_stop")


# Every tool that ends this worker's responsibility for the card, not just the two that
# close it out: ``kanban_request_review`` moves it to ``review`` (goals.py's continuation /
# finalize prompts tell builders to call it) and ``kanban_request_changes`` returns it to
# ``ready`` (the sdlc-review skill tells reviewers to). Nudging after either asks a worker
# that did the right thing to ``kanban_complete`` a card it must not close.
_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
})

# A lane is "kanban-capable" when at least one board tool is bound. The tool names all
# share the prefix; checking the prefix (not a fixed list) keeps the gate honest when the
# toolset grows. Bound tools are exactly ``agent.valid_tool_names`` (agent_init).
_WORKER_TOOL_PREFIX = "kanban_"

# Workspace marker a tool-less lane writes to hand the card off. A FRESH valid marker is
# the only out-of-band terminal state the dispatcher honors — stale or foreign markers are
# ignored so a leftover file can never close next month's respawn.
TERMINAL_MARKER_FILENAME = "kanban-terminal.json"
TERMINAL_MARKER_MAX_AGE_SECONDS = 12 * 3600

_MARKER_ACTIONS = frozenset({"complete", "block"})
_MARKER_KNOWN_KEYS = frozenset({"action", "task_id", "run_token", "reason", "summary", "artifacts", "written_at"})
_MARKER_TEXT_LIMIT = 6000
_MARKER_SUMMARY_LIMIT = 2000
_MARKER_ARTIFACTS_LIMIT = 40

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set for the dispatcher-owned worker, unless
    ``HERMES_KANBAN_STOP_NUDGE`` disables it. In-process delegate_task children and cron runs
    inherit the env var but own no board task and carry no kanban toolset."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool(owned_kanban_task())


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def agent_has_kanban_tools(agent: Any) -> bool:
    """True when this session can call any ``kanban_*`` board tool.

    Reads the agent's bound tool names (``valid_tool_names``), not the configured
    toolset, so a lane whose tools were dropped (muse-bridge, tool-less fallback
    models, sandboxed boards) is correctly reported as unable to comply. Unknown
    binding (``None``) fails OPEN: the classic tool-call nudge is the default.
    """
    names = getattr(agent, "valid_tool_names", None)
    if not names:
        return True  # fail open — unknown binding must not silence the guard
    return any(str(n).startswith(_WORKER_TOOL_PREFIX) for n in names)


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        role = msg.get("role")
        if role == "assistant" and any(
            _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS for tc in msg.get("tool_calls") or []
        ):
            return True
        if role == "tool" and str(msg.get("name") or "") in _TERMINAL_KANBAN_TOOLS:
            return True
    return False


def _marker_path(workspace: Optional[str] = None) -> Optional[str]:
    ws = (workspace or os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not ws:
        return None
    return os.path.join(ws, TERMINAL_MARKER_FILENAME)


def _validate_terminal_marker(
    data: Any, *, task_id: str, expected_run_token: str, path: Optional[str],
) -> Optional[Dict[str, Any]]:
    """The marker dict iff well-formed, fresh, and addressed to THIS run; else None (why: debug log)."""
    if not isinstance(data, dict):
        _log.debug("terminal marker %s: not a JSON object", path)
        return None
    unknown = set(data) - _MARKER_KNOWN_KEYS
    if unknown:
        _log.debug("terminal marker %s: unknown keys %s", path, sorted(unknown))
        return None
    if data.get("action") not in _MARKER_ACTIONS:
        _log.debug("terminal marker %s: bad action", path)
        return None
    if (data.get("task_id") or "").strip() != task_id:
        _log.debug("terminal marker %s: addressed to another task", path)
        return None
    if expected_run_token and (data.get("run_token") or "").strip() != expected_run_token:
        _log.debug("terminal marker %s: run_token mismatch (stale from a prior run)", path)
        return None
    try:
        written_at = float(data.get("written_at") or 0)
    except (TypeError, ValueError):
        written_at = 0.0
    if not written_at or time.time() - written_at > TERMINAL_MARKER_MAX_AGE_SECONDS:
        _log.debug("terminal marker %s: missing or stale written_at", path)
        return None
    for key, limit in (("reason", _MARKER_TEXT_LIMIT), ("summary", _MARKER_SUMMARY_LIMIT)):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            _log.debug("terminal marker %s: bad %s", path, key)
            return None
    artifacts = data.get("artifacts")
    if artifacts is not None:
        if (
            not isinstance(artifacts, list)
            or len(artifacts) > _MARKER_ARTIFACTS_LIMIT
            or not all(isinstance(a, str) for a in artifacts)
        ):
            _log.debug("terminal marker %s: bad artifacts", path)
            return None
    return data


def read_fresh_terminal_marker(
    task_id: Optional[str] = None, *, workspace: Optional[str] = None,
    expected_run_token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The workspace terminal marker if it exists AND is valid and fresh for this run.

    ``workspace``/``expected_run_token`` default to this process's env (worker
    context); the dispatcher passes the task row's ``workspace_path``/``claim_lock``
    explicitly. All checks fail closed: a malformed, stale, foreign-task, or
    prior-run marker reads as None. Never raises.
    """
    path = _marker_path(workspace)
    if not path:
        return None
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not tid:
        return None
    token = (
        expected_run_token
        if expected_run_token is not None
        else (os.environ.get("HERMES_KANBAN_CLAIM_LOCK") or "")
    ).strip()
    try:
        if os.path.getsize(path) > 256 * 1024:
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return _validate_terminal_marker(data, task_id=tid, expected_run_token=token, path=path)


def write_kanban_terminal_marker(
    action: str,
    *,
    summary: Optional[str] = None,
    reason: Optional[str] = None,
    artifacts: Optional[list] = None,
) -> Optional[str]:
    """Write the workspace terminal marker; returns the path, or None when impossible.

    The compliant-exit path for a lane with NO board tools: write this via plain file
    tools, then end. Anything else (drifted run_token, stale marker) is ignored by the
    sweep, so the file must be written fresh by the run that owns the card.
    """
    if action not in _MARKER_ACTIONS:
        return None
    path = _marker_path()
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not path or not task_id:
        return None
    payload: Dict[str, Any] = {
        "action": action,
        "task_id": task_id,
        "run_token": (os.environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip(),
        "written_at": round(time.time(), 3),
    }
    if summary:
        payload["summary"] = str(summary)[:_MARKER_SUMMARY_LIMIT]
    if reason:
        payload["reason"] = str(reason)[:_MARKER_TEXT_LIMIT]
    if artifacts:
        payload["artifacts"] = [str(a) for a in artifacts][:_MARKER_ARTIFACTS_LIMIT]
    try:
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        return None
    return path


def clear_kanban_terminal_marker(*, workspace: Optional[str] = None) -> bool:
    """Remove the workspace terminal marker (the sweep replays it once, then clears it).
    True when a marker file was removed."""
    path = _marker_path(workspace)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
    lane_has_kanban_tools: Optional[bool] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a kanban worker, already completed/blocked, budget exhausted,
    or — on a tool-less lane — a fresh valid terminal marker already hands the card off)."""
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    marker: Optional[Dict[str, Any]] = None
    if lane_has_kanban_tools is False:
        marker = read_fresh_terminal_marker(task_id=tid)
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
        or session_called_kanban_terminal(messages)
        or (lane_has_kanban_tools is False and marker is not None)
    ):
        return None

    if lane_has_kanban_tools is False:
        # Lane cannot call board tools: pointing at kanban_complete would burn both
        # nudge attempts on apologies (the muse-bridge failure mode). The compliant
        # exit is the workspace terminal marker, replayed by the dispatcher's sweep.
        if marker is None:
            complete_example = json.dumps({
                "action": "complete", "task_id": tid,
                "summary": "<1-3 sentence handoff>",
                "artifacts": ["<absolute paths>"],
            })
            block_example = json.dumps({
                "action": "block", "task_id": tid,
                "reason": "<what blocks the card>",
            })
            return (
                "[System: You are a Hermes kanban worker, and this session has NO board "
                "tools (no `kanban_*` tool is available to you) — you CANNOT call "
                "`kanban_complete` or `kanban_block`, and a plain-text reply is NOT a "
                "terminal state for the board.\n\n"
                f"Task `{tid}` must still be handed off. Do this with your file tools, "
                "then end:\n"
                "Write the file `$HERMES_KANBAN_WORKSPACE/kanban-terminal.json` containing "
                f"exactly one JSON object: {complete_example} — or {block_example} if you "
                "are blocked. Write it fresh this turn (an old or edited copy is "
                "rejected), then stop.]\n\n"
                "Never end a turn without either the marker or the tools."
            )
        # Unreachable in practice: a fresh marker short-circuits to None above,
        # so reaching here means no marker exists and the marker nudge fired.
        return None

    # The transcript is the status source: this text is only reached when the session made no
    # handoff call, so it never tells a worker to close a card it already sent to review.
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` has not been handed off: this session made no terminal board "
        "call (`kanban_complete` / `kanban_request_review` / `kanban_request_changes` / "
        "`kanban_block`). Ending now "
        "causes a protocol violation (clean exit with the card still `running`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work is done "
        "and needs no review, `kanban_request_review(summary=...)` if it is a code "
        "change that needs same-card review, OR `kanban_block(reason=...)` if you are "
        "blocked. Reviewers approve with `kanban_complete` or send the card back with "
        "`kanban_request_changes(reason=...)`.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "TERMINAL_MARKER_FILENAME",
    "TERMINAL_MARKER_MAX_AGE_SECONDS",
    "agent_has_kanban_tools",
    "build_kanban_stop_nudge",
    "clear_kanban_terminal_marker",
    "kanban_stop_nudge_enabled",
    "read_fresh_terminal_marker",
    "session_called_kanban_terminal",
    "write_kanban_terminal_marker",
]
