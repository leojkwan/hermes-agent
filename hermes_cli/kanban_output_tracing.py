"""Langfuse tracing for kanban note-surface emissions (observability-only).

Every note emission leaving the dashboard read path — guarded task bodies,
comment bodies, board card previews, deduped activity feeds — is recorded as
a Langfuse trace: one span per readability-pipeline stage plus a
``final_output`` span carrying the complete emitted text. Span inputs carry
the pipeline version (the deterministic analogue of a prompt/template id —
the readability guards are rule-based, there are no model params) plus the
surface and task ids.

Fail-open by design: without the langfuse SDK or credentials every helper is
a pass-through — ``guarded_text`` returns exactly what
``apply_render_guards`` returns, and emission recording is a no-op — so
telemetry can never alter or break the surfaces it observes. The guarded
text is computed BEFORE any tracing runs, so the returned value is
byte-identical with tracing on, off, or broken. Repeat re-renders of
identical content (board refreshes, card re-opens) are deduped per process
by content hash so refreshes don't flood the trace stream; a changed note is
a new emission and traces again.

Credentials resolve through the profile secret scope, falling back to
``LANGFUSE_*``/``HERMES_LANGFUSE_*`` env when no scope machinery is
importable (standalone runs, smoke tests). Under an active profile override
a failed scope read keeps tracing off instead of silently using the launch
profile's credentials — the same multiplex rule the ``observability/langfuse``
plugin follows. Text is capped at ``HERMES_LANGFUSE_MAX_CHARS`` (default
12000, the plugin's ceiling) per captured value; notes sit far below it, so
``final_output`` carries the complete emission in practice.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import logging
import os
import threading
import uuid
from collections import deque
from typing import Any, Optional, Sequence

from hermes_cli.kanban_readability import apply_render_guards

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse
except Exception:  # pragma: no cover - optional dependency
    Langfuse = None

__all__ = [
    "PIPELINE_VERSION",
    "flush_traces",
    "guarded_text",
    "recent_trace_ids",
    "record_emission",
    "reset_client",
]

PIPELINE_VERSION = "readability-render-v1"

_DEFAULT_MAX_CHARS = 12000
_SEEN_CAP = 4096

# Langfuse-issued keys carry these prefixes; anything else is a leftover
# template value the SDK accepts but silently drops at flush time (#23823).
_KEY_PREFIXES = {"public": "pk-lf-", "secret": "sk-lf-"}

# Sentinel: the client slot was tried and failed. Distinguishing it from
# None (= never built) prevents a rebuild attempt on every emission.
_INIT_FAILED = object()

_client_lock = threading.Lock()
_client: Any = None
# Emission dedup: (surface, task_id, content sha) already traced this process.
_seen: set = set()
_seen_lock = threading.Lock()
# (surface, trace_id) of recent emissions — smoke/debug affordance.
_recent_trace_ids: deque = deque(maxlen=64)
_recent_lock = threading.Lock()


def _max_chars() -> int:
    try:
        return int(os.environ.get("HERMES_LANGFUSE_MAX_CHARS", "") or _DEFAULT_MAX_CHARS)
    except ValueError:
        return _DEFAULT_MAX_CHARS


def _truncate(value: str) -> str:
    cap = _max_chars()
    over = len(value) - cap
    return value if over <= 0 else value[:cap] + f"... [truncated {over} chars]"


def _secret(name: str) -> str:
    """Credential read through the profile secret scope, env as fallback.

    Standalone callers (no agent machinery importable, e.g. smoke runs) read
    env directly. Under an active profile override a failed scope read keeps
    tracing off rather than silently using the launch profile's credentials.
    """
    try:
        from agent.secret_scope import get_secret
        from hermes_constants import get_hermes_home_override
    except ImportError:
        return os.environ.get(name, "").strip()
    try:
        return (get_secret(name) or "").strip()
    except Exception:
        if get_hermes_home_override() is None:
            return os.environ.get(name, "").strip()
        return ""


def _base_url() -> str:
    for name in ("HERMES_LANGFUSE_BASE_URL", "HERMES_LANGFUSE_HOST",
                 "LANGFUSE_BASE_URL", "LANGFUSE_HOST"):
        value = _secret(name)
        if value:
            return value
    return "https://cloud.langfuse.com"


def _build_client():
    if Langfuse is None:
        return None
    public_key = _secret("HERMES_LANGFUSE_PUBLIC_KEY") or _secret("LANGFUSE_PUBLIC_KEY")
    secret_key = _secret("HERMES_LANGFUSE_SECRET_KEY") or _secret("LANGFUSE_SECRET_KEY")
    if not (public_key and secret_key):
        return None
    if not (public_key.startswith(_KEY_PREFIXES["public"])
            and secret_key.startswith(_KEY_PREFIXES["secret"])):
        logger.warning(
            "kanban output tracing: credentials lack the pk-lf-/sk-lf- prefix; "
            "tracing stays off (the SDK would silently drop every trace).")
        return None
    try:
        client = Langfuse(public_key=public_key, secret_key=secret_key, base_url=_base_url())
    except Exception as exc:  # pragma: no cover - fail-open
        logger.warning("kanban output tracing: Langfuse init failed: %s", exc)
        return None
    # atexit is LIFO: registered AFTER the SDK's own hook, so this final flush
    # runs before SDK shutdown and short-lived processes still export.
    atexit.register(flush_traces)
    return client


def _settled() -> Any:
    """Current client slot without building: a client, ``_INIT_FAILED`` or None."""
    return _client


def _get_client():
    global _client
    if _client is not None:
        return None if _client is _INIT_FAILED else _client
    with _client_lock:
        if _client is None:
            built = _build_client()
            _client = built if built is not None else _INIT_FAILED
    return None if _client is _INIT_FAILED else _client


def reset_client() -> None:
    """Test isolation: drop the settled client slot and emission dedup state."""
    global _client
    with _client_lock:
        _client = None
    with _seen_lock:
        _seen.clear()


def _first_emission(key: tuple) -> bool:
    with _seen_lock:
        if key in _seen:
            return False
        if len(_seen) >= _SEEN_CAP:
            _seen.clear()
        _seen.add(key)
        return True


def _emission_key(surface: str, task_id: str, content: str) -> tuple:
    digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
    return (surface, task_id, digest)


def _emit_emission_trace(client, *, surface: str, task_id: str, raw_chars: int,
                         output: Optional[str], counts: Optional[dict] = None,
                         stages: Sequence[tuple[str, str]] = ()) -> None:
    """Best-effort trace emission; never raises (telemetry must not break reads)."""
    try:
        trace_id = client.create_trace_id(seed=f"kanban::{surface}::{uuid.uuid4().hex}")
        trace_ctx: dict[str, Any] = {"trace_id": trace_id}
        if task_id:
            trace_ctx["session_id"] = f"kanban-task-{task_id}"
        metadata = {
            "source": "hermes-kanban", "pipeline": PIPELINE_VERSION, "surface": surface,
            "task_id": task_id, "input_chars": raw_chars,
            # Rule-based guards: no model params exist to attach.
            "deterministic": True,
        }
        ctx = client.start_as_current_observation(
            trace_context=trace_ctx, name=f"kanban.output.{surface}", as_type="chain",
            input={"surface": surface, "task_id": task_id, "chars": raw_chars},
            metadata=metadata, end_on_exit=False)
        root = ctx.__enter__()
        try:
            prev_chars = raw_chars
            for stage_name, stage_text in stages:
                stage_span = root.start_observation(
                    name=f"stage.{stage_name}", as_type="span",
                    input={"chars_in": prev_chars}, metadata={"stage": stage_name})
                stage_span.update(output=_truncate(stage_text))
                stage_span.end()
                prev_chars = len(stage_text)
            final_output = _truncate(output) if output is not None else (counts or {})
            final = root.start_observation(
                name="final_output", as_type="span", input={"chars_in": raw_chars},
                metadata={"complete_output": output is None or len(output) <= _max_chars()})
            final.update(output=final_output)
            final.end()
            root.end()
        finally:
            with contextlib.suppress(Exception):
                ctx.__exit__(None, None, None)
        with _recent_lock:
            _recent_trace_ids.append((surface, trace_id))
    except Exception as exc:  # pragma: no cover - fail-open
        logger.debug("kanban output tracing: emit failed for %s: %s", surface, exc)


def guarded_text(raw: Optional[str], *, surface: str, task_id: str = "") -> Optional[str]:
    """``apply_render_guards(raw)`` with per-stage + final-output tracing.

    The guarded text is computed exactly once, before any tracing runs, so
    the returned value is byte-identical with tracing on, off, or broken.
    Tracing is best-effort and deduped per (surface, task_id, output content).
    """
    if not raw:
        return apply_render_guards(raw)
    client = _get_client()
    if client is None:
        return apply_render_guards(raw)
    stages: list[tuple[str, str]] = []
    result = apply_render_guards(raw, stage_observer=lambda name, text: stages.append((name, text)))
    if _first_emission(_emission_key(surface, task_id, result or "")):
        _emit_emission_trace(client, surface=surface, task_id=task_id, raw_chars=len(raw),
                             output=result or "", stages=stages)
    return result


def record_emission(*, surface: str, task_id: str = "", output: Optional[str] = None,
                    counts: Optional[dict] = None, dedup: bool = True) -> None:
    """Record one surface emission as its own trace.

    ``output`` carries the emitted text — the ``final_output`` span gets it
    complete; ``counts`` carries count-only emissions (activity dedup) so raw
    event payloads never hit the trace stream. One of the two must be
    present. Deduped per (surface, task_id, content) unless ``dedup=False``.
    """
    if not output and not counts:
        return
    client = _get_client()
    if client is None:
        return
    content = output if output is not None else json.dumps(counts, sort_keys=True, default=str)
    if dedup and not _first_emission(_emission_key(surface, task_id, content)):
        return
    _emit_emission_trace(client, surface=surface, task_id=task_id,
                         raw_chars=len(output) if output is not None else 0,
                         output=output, counts=counts)


def flush_traces() -> None:
    """Flush the settled client, if any (never builds one)."""
    client = _settled()
    if client is not None and client is not _INIT_FAILED:
        with contextlib.suppress(Exception):
            client.flush()


def recent_trace_ids() -> list[tuple[str, str]]:
    with _recent_lock:
        return list(_recent_trace_ids)
