"""Render-time readability guards for kanban note surfaces.

The Shadow-note readability spec (six-section canonical template, plain-language
rules) is applied as presentation-only transforms at the read boundary: stored
card bodies, comments, and task_events are never rewritten in the database.
Three layers:

- ``compose_note`` builds a canonical note from structured sections (authoring).
- ``apply_render_guards`` normalizes freehand text at display time — entity-hash
  truncation, one-time row-token expansion, bare absolute paths to labeled
  links, board-chrome removal.
- ``dedup_activity_events`` collapses the verified double-stamped blocked
  transition (a ``created`` event with ``status=blocked`` plus a ``blocked``
  event with ``reason=initial_status`` sharing one timestamp) from activity
  feeds.

Guards are deliberately no-ops on already-conforming text: the golden test
round-trips the spec's canonical ~r334 note through compose + guards unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Collection, Mapping, Optional, Sequence, TypeVar, Union, overload

_T = TypeVar("_T", bound=Mapping)

__all__ = [
    "SECTION_ORDER",
    "apply_render_guards",
    "blocked_creator_keys",
    "compose_note",
    "dedup_activity_events",
    "strip_board_chrome",
]

SectionValue = Union[str, Sequence[str]]

SECTION_ORDER: tuple[str, ...] = (
    "STATUS",
    "BLOCKED ON / GATE",
    "NEXT ACTION",
    "KEY LINKS",
    "CORRECTIONS CHECKLIST",
    "NOTES / PARKED",
)

_EMPTY_FILLER = "None."
_NOT_BLOCKED_FILLER = "Not blocked."

_HEX64 = re.compile(r"(?<![\w/])[0-9a-fA-F]{64}(?![\w/])")
_ROW_TOKEN_WITH_ROW = re.compile(r"\brow\s+(~r\d+)\b")
_ROW_TOKEN_BARE = re.compile(r"(?<![\w~])(~r\d+)\b")
# Absolute (~/-rooted) path tokens. The lookbehind keeps mid-word and
# already-matched slashes out ("docs/design/x" never matches — relative paths
# stay the authoring layer's job), so labeled-link targets are untouched.
_PATH_TOKEN = re.compile(r"(?<![\w/])(?:~/|/)(?:[\w.@+-]+/?)+")

# Full-line board chrome (spec rule 8): UI furniture captured into note bodies —
# estimate buttons, comment/activity/attachment counters, transition stamps,
# relative timestamps. Anchored full-line matches so real prose never collides.
_CHROME_LINE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"ESTIMATE",
        r"Estimate effort",
        r"makes a model call",
        r"COMMENTS\s*·\s*\d+",
        r"Comment",
        r"ACTIVITY\s*·\s*\d+",
        r"ATTACHMENTS\s*·\s*\d+",
        r"created in \S+",
        r"blocked — needs human input",
        r"initial_status",
        r"No attachments yet\.?",
        r"\d+\s*(?:min|hr|hour|day|week)s?\.?\s*ago",
    )
)


def compose_note(sections: Mapping[str, SectionValue]) -> str:
    """Render structured sections as the canonical six-section note.

    Keys must be ``SECTION_ORDER`` names. A ``str`` value passes through
    verbatim (multi-line allowed); list values render one entry per line —
    ``CORRECTIONS CHECKLIST`` as checkable ``- [ ]`` items, ``NOTES / PARKED``
    as bullets, ``KEY LINKS`` entries carrying their own ``[label] → target``
    shape. Empty sections print one honest filler line so the structure never
    varies between notes.
    """
    unknown = [name for name in sections if name not in SECTION_ORDER]
    if unknown:
        raise ValueError(f"unknown note sections {unknown}; expected {list(SECTION_ORDER)}")
    blocks = [f"{name}\n{_section_body(name, sections.get(name))}" for name in SECTION_ORDER]
    return "\n\n".join(blocks)


StageObserver = Callable[[str, str], None]
"""Observability hook shape: ``(stage_name, text_after_stage)`` per guard."""


@overload
def apply_render_guards(text: str, *, stage_observer: Optional[StageObserver] = ...) -> str: ...


@overload
def apply_render_guards(text: None, *, stage_observer: Optional[StageObserver] = ...) -> None: ...


def apply_render_guards(text: Optional[str],
                        *, stage_observer: Optional[StageObserver] = None) -> Optional[str]:
    """Normalize one freehand note for display (presentation only).

    Order: hash truncation → row-token expansion → path labeling → chrome
    strip. Each guard is a no-op on already-conforming text. When
    ``stage_observer`` is given it is called after each transform —
    observability only; the returned value is computed from the pipeline
    alone.
    """
    if not text:
        return text
    guarded = _HEX64.sub(lambda match: match.group(0)[:8], text)
    if stage_observer:
        stage_observer("hash_truncate", guarded)
    guarded = _expand_row_token(guarded)
    if stage_observer:
        stage_observer("row_expansion", guarded)
    guarded = _label_bare_paths(guarded)
    if stage_observer:
        stage_observer("path_labeling", guarded)
    guarded = strip_board_chrome(guarded)
    if stage_observer:
        stage_observer("chrome_strip", guarded)
    return guarded


def _expand_row_token(text: str) -> str:
    """One-time glossary expansion of the bare row token (spec rule 2).

    Notes that already carry the labeled form — the canonical KEY LINKS
    ``[Shadow row] → …`` line or any prose naming the plan — skip expansion
    so conforming text is never mangled.
    """
    if "Shadow plan" in text or "plan row" in text:
        return text
    for pattern, replacement in (
        (_ROW_TOKEN_WITH_ROW, r"Shadow plan row (\1)"),
        (_ROW_TOKEN_BARE, r"\1 (a Shadow plan row)"),
    ):
        expanded, count = pattern.subn(replacement, text, count=1)
        if count:
            return expanded
    return text


def strip_board_chrome(text: str) -> str:
    """Drop board-UI chrome lines captured into a note body (spec rule 8)."""
    kept = [
        line
        for line in text.split("\n")
        if not any(pattern.fullmatch(line.strip()) for pattern in _CHROME_LINE_PATTERNS)
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def dedup_activity_events(
    events: Sequence[_T],
    prior_blocked_creators: Collection[tuple] = (),
) -> list[_T]:
    """Collapse the verified initial-status double stamp from activity feeds.

    Card creation straight into ``blocked`` stamps the transition twice — a
    ``created`` event with ``payload.status == "blocked"`` and a ``blocked``
    event with ``payload.reason == "initial_status"`` at the same
    ``created_at`` — so readers see one transition as two lines. The
    ``created`` event is kept, its ``blocked`` twin dropped. Keys are
    ``(task_id, created_at)``: the WS tailer streams events across the whole
    board, so a bare timestamp could cross-drop two tasks created the same
    second. ``prior_blocked_creators`` carries keys from earlier stream
    batches so a twin split across batches is still suppressed. Read-path
    only: task_events rows are untouched. Genuine blocks (different reason,
    different timestamp, or no created twin) always stay.
    """
    known = set(prior_blocked_creators) | blocked_creator_keys(events)
    return [
        event
        for event in events
        if not (
            event.get("kind") == "blocked"
            and _event_payload(event).get("reason") == "initial_status"
            and (event.get("task_id"), event.get("created_at")) in known
        )
    ]


def blocked_creator_keys(events: Sequence[Mapping]) -> set[tuple]:
    """Dedup anchors: ``(task_id, created_at)`` of created-into-blocked events."""
    return {
        (event.get("task_id"), event.get("created_at"))
        for event in events
        if event.get("kind") == "created" and _event_payload(event).get("status") == "blocked"
    }


def _event_payload(event: Mapping) -> dict:
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    return payload if isinstance(payload, dict) else {}


def _section_body(name: str, value: Optional[SectionValue]) -> str:
    if value is None:
        return _filler(name)
    if isinstance(value, str):
        body = value.strip("\n")
        return body if body else _filler(name)
    entries = [str(entry).strip("\n") for entry in value if str(entry).strip()]
    if not entries:
        return _filler(name)
    if name == "CORRECTIONS CHECKLIST":
        entries = [f"- [ ] {entry}" for entry in entries]
    elif name == "NOTES / PARKED":
        entries = [f"- {entry}" for entry in entries]
    return "\n".join(entries)


def _filler(name: str) -> str:
    return _NOT_BLOCKED_FILLER if name == "BLOCKED ON / GATE" else _EMPTY_FILLER


def _label_bare_paths(text: str) -> str:
    return "\n".join(_label_line_paths(line) for line in text.split("\n"))


def _label_line_paths(line: str) -> str:
    spans = _paren_spans(line)

    def convert(match: re.Match[str]) -> str:
        if any(start <= match.start() < end for start, end in spans):
            return match.group(0)
        if line[: match.start()].rstrip().endswith("→"):
            return match.group(0)  # already a labeled link's target
        path = match.group(0)
        label = path.rstrip("/").rsplit("/", 1)[-1]
        return f"[{label}] → {path}"

    return _PATH_TOKEN.sub(convert, line)


def _paren_spans(line: str) -> list[tuple[int, int]]:
    """Spans of balanced ``(...)`` groups; paths inside parens are prose, not pointers."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for i, ch in enumerate(line):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0:
                spans.append((start, i + 1))
    return spans
