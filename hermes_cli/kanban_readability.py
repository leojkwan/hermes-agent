"""Render-time readability guards for kanban note surfaces.

The Shadow-note readability spec (six-section canonical template, plain-language
rules) is applied as presentation-only transforms at the read boundary: stored
card bodies, comments, and task_events are never rewritten in the database.
Three layers:

- ``compose_note`` builds a canonical note from structured sections (authoring).
- ``recompose_note`` imposes the same template on freehand text at the read
  boundary — concerns are routed to sections, never invented (t_ca4e2742).
- ``apply_render_guards`` normalizes freehand text at display time — entity-hash
  truncation, one-time row-token/task-id expansion, bare absolute paths (even
  inside prose parentheses) to labeled links, board-chrome removal.
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
    "recompose_note",
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
_ROW_TOKEN_BARE = re.compile(r"(?<![\w~])(~r\d+)\b")
# Absolute (~/-rooted) path tokens. The lookbehind keeps mid-word and
# already-matched slashes out ("docs/design/x" never matches — relative paths
# stay the authoring layer's job), so labeled-link targets are untouched.
# Route/command-looking tokens (/v1/models, /amplify) get labeled like any
# other path: the rubric counts them bare otherwise, and a rubric/scorer
# carve-out would mean editing the frozen measuring stick (t_ca4e2742 D).
_PATH_TOKEN = re.compile(r"(?<![\w/])(?:~/|/)(?:[\w.@+-]+/?)+")

_TASK_ID = re.compile(r"\bt_[0-9a-f]{8,}\b")
_TASK_WORD = re.compile(r"\btask\b", re.IGNORECASE)

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

    Order: template recomposition → hash truncation → row-token expansion →
    task-id expansion → path labeling → chrome strip. Each guard is a no-op on
    already-conforming text. When ``stage_observer`` is given it is called
    after each transform — observability only; the returned value is computed
    from the pipeline alone.
    """
    if not text:
        return text
    guarded = recompose_note(text)
    if stage_observer:
        stage_observer("recompose", guarded)
    guarded = _HEX64.sub(lambda match: match.group(0)[:8], guarded)
    if stage_observer:
        stage_observer("hash_truncate", guarded)
    guarded = _expand_row_token(guarded)
    if stage_observer:
        stage_observer("row_expansion", guarded)
    guarded = _expand_task_id(guarded)
    if stage_observer:
        stage_observer("task_id_expansion", guarded)
    guarded = _label_bare_paths(guarded)
    if stage_observer:
        stage_observer("path_labeling", guarded)
    guarded = strip_board_chrome(guarded)
    if stage_observer:
        stage_observer("chrome_strip", guarded)
    return guarded


_ROW_EXPANSION_RX = re.compile(
    r"shadow plan row|shadow entity|shadow row|plan row|\(~r\d+\)", re.I
)


def _expand_row_token(text: str) -> str:
    """One-time glossary expansion of the bare row token (spec rule 2).

    Skip rule mirrors the scorer's: expansion is redundant only when the FIRST
    row-token sentence already reads expanded ("Shadow entity …", a
    parenthesized row gloss, …) or the note carries a labeled ``] →`` glossary
    line mapping the row (the golden's KEY LINKS ``[Shadow row] → ~r334 …``).
    The gloss lands at the FIRST occurrence in reading order — the scorer
    judges that occurrence, not whichever one a pattern happens to hit first.
    """
    match = _ROW_TOKEN_BARE.search(text)
    if not match:
        return text
    sentence = next((s for s in _sentences(text) if _ROW_TOKEN_BARE.search(s)), "")
    if _ROW_EXPANSION_RX.search(sentence):
        return text
    for line in text.split("\n"):
        if "] \u2192" in line and "~r" in line and "shadow" in line.lower():
            return text

    def gloss(m: re.Match[str]) -> str:
        if re.search(r"row\s+$", text[: m.start()]):
            return f"Shadow plan row ({m.group(1)})"
        return f"{m.group(1)} (a Shadow plan row)"

    expanded, count = _ROW_TOKEN_BARE.subn(gloss, text, count=1)
    return expanded if count else text


def _expand_task_id(text: str) -> str:
    """One-time glossary expansion of the first bare kanban task id.

    Mirrors ``_expand_row_token`` for the ``t_[0-9a-f]{8,}`` class (rubric
    §3.2): the first occurrence gains ``(a kanban task)`` unless the sentence
    already carries the expansion (the word ``task`` — ``card`` and ``cards``
    do not count per the rubric) or the id already sits in a parenthesized
    gloss. Later occurrences are unconstrained.
    """
    match = _TASK_ID.search(text)
    if not match:
        return text
    sentence = next((s for s in _sentences(text) if _TASK_ID.search(s)), "")
    if _TASK_WORD.search(sentence):
        return text
    tail = text[match.end():]
    stripped = tail.lstrip(" \n")
    if stripped.startswith("("):
        return text  # already a parenthesized gloss
    head = text[: match.end()]
    if head.rstrip().endswith("(") or _inside_unclosed_paren(head):
        # The id sits inside a paren group like "(card t_fb55c86f)": close the
        # group first, then gloss — never nest.
        close = tail.find(")")
        if close != -1:
            at = match.end() + close + 1
            return text[:at] + " (a kanban task)" + text[at:]
    return head + " (a kanban task)" + tail


def _inside_unclosed_paren(head: str) -> bool:
    depth = 0
    for ch in head:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
    return depth > 0


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


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


# --- recomposition (t_ca4e2742): template composition for freehand bodies ---
#
# Route-only, non-fabricating: freehand concerns are sorted into the canonical
# sections, kept in their own words, one concern per line. Nothing is
# paraphrased, summarized, or invented; a section with nothing found prints the
# honest filler via compose_note. Notes that already carry two exact section
# header lines pass through untouched (the golden note must stay byte-exact).

_SECTION_HEADER_RX = re.compile(
    r"^\s*(STATUS|BLOCKED ON / GATE|NEXT ACTION|KEY LINKS|CORRECTIONS CHECKLIST|"
    r"NOTES[/\\] ?PARKED|NOTES / PARKED)\s*:?\s*$",
    re.I,
)
_LABEL_LINE_RX = re.compile(r"^\s*\[[^\]\n]+\]\s*(?:\u2192|->)\s*")
_PATHISH_RX = re.compile(
    r"(?<![\w/])(?:~/|/)(?:[\w.@+-]+/?)+"
    r"|\b[\w.-]+\.(?:md|html?|py|json|ya?ml|sh|txt|toml|cfg|ini|sqlite|db|pdf|png|jpe?g|gif|mp4|mov|fcpxmld)\b"
)
_RECEIPT_LINE_RX = re.compile(r"^\s*(?:t_[0-9a-f]{8,}|~r\d+)\s+\u2014\s+")
_STRUCTURED_PREFIX_RX = re.compile(
    r"^(?:goal|context|approach|steps?|acceptance(?:\s+criteria)?|deliverables?|"
    r"output|background|source(?:\s+content)?|summary|outcome|now|risk|decision|"
    r"note|notes)\s*:\s*",
    re.I,
)
_GATE_RX = re.compile(
    r"\bgate\b|\bblocked\b|\bwaiting on\b|\bfreezes?\b|\bstale\b|\btrap\b"
    r"|\bwalkthrough\b|\bapproval\b|\bacceptance\b",
    re.I,
)
_PARKED_RX = re.compile(r"\bparked\b|\bdeferred\b|\bheld back\b|\bout of scope\b", re.I)
_ACTION_RX = re.compile(
    r"^(?:read|run|review|verify|survey|check|open|locate|probe|define|write|add|"
    r"rewrite|map|identify|confirm|produce|draft|shortlist|return|go|cut|schedule|"
    r"perform|fill|emit|triage|keep|unblock|eliminate|re-?establish|implement|"
    r"close|complete|decide|create|install|integrate|build|compare|rebuild|"
    r"publish|update|stage|land|record|mark|drop|move|ensure|document|commit|"
    r"resume|deliver|show|set|enumerate|query)\b",
    re.I,
)
_SENT_SPLIT_RX = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9~`\[])|(?<=;)\s+")
_LIST_ITEM_RX = re.compile(r"^\s*(?:[-*\u2022\u2023\u25e6\u2043\u2219]|\d+[.)])\s+(.*)$")

# A checklist sentence names its items in a trailing parenthetical, so the whole
# line routes to KEY LINKS on its path and CORRECTIONS CHECKLIST prints "None." —
# the buried-checklist failure this template exists to remove. Splitting is keyed
# on the LABEL, never on "a paren holding commas": the same note carries
# ``…/scan-wait-r1/ (contract, prototype, state images)`` on its gate line, and a
# paren-shaped heuristic would shred that into bullets and make the gate worse.
_CHECKLIST_LABEL_RX = re.compile(r"^(?:corrections\s+checklist|checklist|fixes|to-?dos?)\s*:", re.I)
_TRAILING_PAREN_RX = re.compile(r"\s*\(([^()]*)\)\s*\.?\s*$")
_CHECKLIST_ITEM_MAX = 60


def _split_checklist_line(body: str) -> tuple[Optional[str], list[str]]:
    """Split ``Checklist: <path> (a, b, c)`` into its link half and its items.

    Returns ``(link_text, items)``; ``(None, [])`` when the line is not a
    checklist enumeration, so the caller falls through to normal routing. Every
    item is a verbatim substring of the author's own words — this re-routes, it
    never paraphrases or invents.
    """
    if not _CHECKLIST_LABEL_RX.match(body):
        return None, []
    match = _TRAILING_PAREN_RX.search(body)
    if not match:
        return None, []
    items = [part.strip() for part in re.split(r"[,;]", match.group(1)) if part.strip()]
    # Guards: an enumeration, not prose. Three-plus short fragments, none of
    # which carries sentence-terminal punctuation.
    if len(items) < 3:
        return None, []
    if any(len(item) > _CHECKLIST_ITEM_MAX or item.endswith((".", "!", "?")) for item in items):
        return None, []
    return body[: match.start()].strip() or None, items


_ACTION_CONNECTIVE_RX = re.compile(
    r"^(?:then|next|also|first|finally|afterwards)\b[,:;]?\s+", re.I
)


def _is_action_sentence(body: str) -> bool:
    """Imperative match, including after a leading sequencing connective.

    ``Then run the walkthrough.`` is an instruction, not a gate — but a
    position-0 verb match misses it and the gate regex then eats it on
    ``walkthrough``, flipping an actionable note to Blocked.
    """
    return bool(_ACTION_RX.match(body) or _ACTION_RX.match(_ACTION_CONNECTIVE_RX.sub("", body, count=1)))


def _looks_structured(text: str) -> bool:
    return sum(1 for ln in text.split("\n") if _SECTION_HEADER_RX.match(ln)) >= 2


def _normalize_source_line(line: str) -> Optional[str]:
    line = line.replace("\t", " ").replace("\u00a0", " ").rstrip()
    if not line.strip():
        return None
    if any(p.fullmatch(line.strip()) for p in _CHROME_LINE_PATTERNS):
        return None
    return line


def _prose_sentences(line: str) -> list[str]:
    return [p.strip() for p in _SENT_SPLIT_RX.split(line) if p and p.strip()]


def _strip_structured_prefix(sentence: str) -> str:
    body = _STRUCTURED_PREFIX_RX.sub("", sentence, count=1).strip()
    if not body:
        return ""
    return body[0].upper() + body[1:]


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ensure_period(sentence: str) -> str:
    # A clause split off mid-sentence keeps its ';'/',' and reads as a fragment;
    # the terminator belongs to the sentence it was cut from, not to this line.
    sentence = _collapse(sentence).rstrip().rstrip(";,")
    return sentence if sentence.endswith((".", "!", "?")) else sentence + "."


def _status_line(action_lines: Sequence[str], *, gated: bool = False, parked: bool = False) -> str:
    """One honest line about the note's own routing outcome.

    A note whose content routed to BLOCKED ON / GATE is not "In progress" — it is
    waiting on someone. Claiming progress on gated work is the exact dishonesty
    this template exists to remove, so STATUS is derived from where the content
    actually landed, never assumed.
    """
    if gated:
        return "Blocked — awaiting gate."
    if action_lines and _ACTION_RX.match(action_lines[0]):
        return "Ready to start."
    if parked:
        return "Parked."
    return "In progress."


# Recomposition applies to note-length bodies only: a one-line comment
# templated into six sections (five ``None.`` fillers) is worse, not better.
# Shortest corpus note is ~290 chars; comment-length texts stay on the
# token guards alone.
_WORTH_STRUCTURING = 240


def recompose_note(text: str) -> str:
    """Impose the canonical template on freehand text (read-path only).

    A note already carrying two exact section header lines is returned
    unchanged, and so is comment-length text (below ``_WORTH_STRUCTURING``).
    Otherwise each source line lands in exactly one place: labeled
    ``[label] →`` lines, board receipt rows (``t_… —``, ``~rN —``), and
    path-bearing reference sentences go to KEY LINKS verbatim; list items under
    a checklist header keep their text under CORRECTIONS CHECKLIST; parked
    items go to NOTES / PARKED; imperative sentences route to NEXT ACTION;
    gate/state sentences to BLOCKED ON / GATE. Concerns are routed in their own
    words — never paraphrased, never invented. STATUS states the first action's
    readiness ("Ready to start." / "In progress.").
    """
    if len(text) < _WORTH_STRUCTURING or _looks_structured(text):
        return text
    lines = [ln for ln in (_normalize_source_line(raw) for raw in text.split("\n")) if ln]
    if not lines:
        return text
    gate: list[str] = []
    actions: list[str] = []
    parked: list[str] = []
    links: list[str] = []
    checklist: list[str] = []
    current: Optional[str] = None  # None | "checklist" | "parked" | "links"
    for line in lines:
        header = _SECTION_HEADER_RX.match(line)
        if header:
            name = header.group(1).upper()
            if name.startswith("NOTES"):
                current = "parked"
            elif name == "CORRECTIONS CHECKLIST":
                current = "checklist"
            elif name == "KEY LINKS":
                current = "links"
            else:
                current = None
            continue
        if current == "checklist":
            item = _LIST_ITEM_RX.match(line)
            checklist.append(_collapse(item.group(1) if item else line))
            continue
        if current == "parked":
            item = _LIST_ITEM_RX.match(line)
            parked.append(_collapse(item.group(1) if item else line))
            continue
        if current == "links":
            links.append(line)
            continue
        if _LABEL_LINE_RX.match(line) or _RECEIPT_LINE_RX.match(line):
            links.append(line)
            continue
        for sentence in _prose_sentences(line):
            prefix = _STRUCTURED_PREFIX_RX.match(sentence)
            body = _strip_structured_prefix(sentence)
            marker = _LIST_ITEM_RX.match(body)
            if marker:
                body = marker.group(1).strip()
                body = body[:1].upper() + body[1:] if body else body
            if not body:
                continue
            if _LABEL_LINE_RX.match(body):
                links.append(body)
                continue
            link_half, items = _split_checklist_line(body)
            if items:
                checklist.extend(items)
                if link_half:
                    # Verbatim, like every other KEY LINKS entry. Adding a period
                    # here puts it *inside* the filename that path labeling later
                    # matches, yielding "[r334-corrections.md.]".
                    links.append(link_half)
                continue
            if prefix and prefix.group(0).lower().startswith(("acceptance", "deliverable")):
                checklist.append(_collapse(body))  # acceptance criteria are checkable items
                continue
            if _is_action_sentence(body):
                actions.append(body)
                continue
            if _PATHISH_RX.search(body):
                links.append(body)  # reference sentence: keep verbatim for KEY LINKS
                continue
            if _GATE_RX.search(body):
                if gate:
                    parked.append(body)  # one gate state per note; extras stay visible, parked
                else:
                    gate.append(body)
                continue
            parked.append(body)  # context/notes default: never a fabricated gate claim
    sections: dict[str, SectionValue] = {
        "STATUS": _status_line(actions, gated=bool(gate), parked=bool(parked))
    }
    if gate:
        sections["BLOCKED ON / GATE"] = "\n".join(_ensure_period(s) for s in gate)
    if actions:
        sections["NEXT ACTION"] = "\n".join(_ensure_period(_collapse(a)) for a in actions)
    elif gate:
        # Navigational chrome, the same class of thing the "None." filler already
        # is — it points at the section that holds the answer. Synthesizing an
        # imperative out of the gate sentence would be paraphrase, which this
        # layer does not do.
        sections["NEXT ACTION"] = "Blocked — see BLOCKED ON / GATE."
    if links:
        sections["KEY LINKS"] = links
    if checklist:
        sections["CORRECTIONS CHECKLIST"] = checklist
    if parked:
        sections["NOTES / PARKED"] = [_ensure_period(p) for p in parked]
    return compose_note(sections)


def _label_bare_paths(text: str) -> str:
    labeled_prev = False
    out = []
    for line in text.split("\n"):
        out.append(_label_line_paths(line, prev_line_labeled=labeled_prev))
        labeled_prev = "] \u2192" in line
    return "\n".join(out)


def _label_line_paths(line: str, *, prev_line_labeled: bool = False) -> str:
    # Paren policy (t_ca4e2742 D decision, scorer-shaped): a REAL file path is
    # labeled wherever it appears — including inside prose parentheses. A
    # paren-wrapped path is restructured into the labeled-link shape the
    # reader (and the rubric) expects, keeping every prose word:
    #   ``(e.g., GET /v1/models)``  →  ``[models] → /v1/models (e.g., GET)``
    # Exemptions keep conforming text byte-exact: a paren group on a labeled
    # line or its wrapped continuation (the golden's ``(inside the r334 clean
    # snapshot ~/.shadow/clean/...)`` — a second label on the same path is
    # pure noise), and labeled-link targets themselves.
    p_open = _paren_open_spans(line)
    if "] \u2192" in line or prev_line_labeled:
        return _label_line_inline(line, p_open, exempt_parens=True)
    for o, e in p_open:
        inner = line[o + 1:e - 1]
        wrapped = _PATH_TOKEN.search(inner)
        if wrapped:
            return _restructure_wrapped_path(line, o, e, inner, wrapped)
    return _label_line_inline(line, p_open, exempt_parens=False)


def _label_line_inline(line: str, p_open: list[tuple[int, int]], *, exempt_parens: bool) -> str:
    def convert(match: re.Match[str]) -> str:
        if line[: match.start()].rstrip().endswith("\u2192"):
            return match.group(0)  # already a labeled link's target
        if exempt_parens and any(o <= match.start() < e for o, e in p_open):
            return match.group(0)  # continuation inside a labeled line's paren group
        path = match.group(0)
        label = path.rstrip("/").rsplit("/", 1)[-1]
        return f"[{label}] \u2192 {path}"

    return _PATH_TOKEN.sub(convert, line)


def _restructure_wrapped_path(line: str, o: int, e: int,
                              inner: str, wrapped: re.Match[str]) -> str:
    path = wrapped.group(0)
    label = path.rstrip("/").rsplit("/", 1)[-1]
    kept = re.sub(r"\s+", " ", inner[:wrapped.start()] + " " + inner[wrapped.end():]).strip()
    rebuilt = line[:o] + f"[{label}] \u2192 {path}"
    if kept:
        rebuilt += f" ({kept})"
    rebuilt += line[e:]
    # Second pass labels any remaining bare paths; the new continuation group
    # is exempt (the line now carries the label arrow).
    return _label_line_inline(rebuilt, _paren_open_spans(rebuilt), exempt_parens=True)


def _paren_open_spans(line: str) -> list[tuple[int, int]]:
    """Spans from each ``(`` to the end of its balanced ``(...)`` group."""
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
    if depth:  # unclosed group: treat as open to end of line
        spans.append((start, len(line)))
    return spans
