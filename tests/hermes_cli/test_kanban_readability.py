"""Render-time readability guards for kanban notes (hermes_cli/kanban_readability.py).

Golden case: the readability spec's canonical ~r334 note, composed from
structured fields and passed through the render guards, reproduces the spec's
AFTER block verbatim — proving both the composer and that the guards are no-ops
on already-conforming text.
"""

from __future__ import annotations

import pytest

from hermes_cli.kanban_readability import (
    SECTION_ORDER,
    apply_render_guards,
    compose_note,
    dedup_activity_events,
    strip_board_chrome,
)

GOLDEN_AFTER = """STATUS
Corrections on the ~r334 scan-wait review package are confirmed and fix-ready;
waiting on Leo's acceptance walkthrough. Nothing else blocks.

BLOCKED ON / GATE
Leo's approval — one ~8-minute in-chat walkthrough of the review package
(contract, prototype, state art). Leo says "accept" or names corrections per
state; ~r335 stays parked until then.

NEXT ACTION
Leo: open the package and run the 8-step walkthrough; accept or dictate
corrections per state.

KEY LINKS
[Review package (contract, prototype, state art)] → docs/design/review-center/scan-wait-r1/
  (inside the r334 clean snapshot ~/.shadow/clean/resplit-r334-20260920-final)
[Corrections checklist — 8 items, each with its fix] → r334-corrections.md
[Prototype (self-contained, no server)] → prototype.html (same folder)
[Shadow row] → ~r334 on Shadow plan entity 9c83f1f4

CORRECTIONS CHECKLIST
- [ ] 1. Spanish title collision — "start" and "completion" screens share one
      Spanish title/VO line. Fix drafted: start becomes "Listo para empezar."
- [ ] 2. SVG muted-text contrast — 5 state images fail 4.5:1 (#617276 on paper
      = 4.35). Fix drafted: swap to #596a6d (passes everywhere).
- [ ] 3. SVG gold contrast — slow-stalled only: #a66b13 fails on paper (3.84).
      Fix drafted: #8a5c0e (passes, keeps the warn-gold look).
- [ ] 4. "Still working." honesty — the screen can't observe the provider
      working. Proposed: "Still waiting." Leo picks the final wording.
- [ ] 5. Done button under 44pt — 38px today. Fix drafted: 44px, one line of CSS.
- [ ] 6. Transition map gaps — 4 Close→ edges missing from the contract map.
      Fix drafted: append the 5-line map block.
- [ ] 7. Dark-theme review gap — colors pass; dark is only reachable on 2 of 7
      states. Option A (Theme selector, small code) vs B (checklist note). Leo calls it.
- [ ] 8. Fact rotation untestable — prototype shows one static fact. Option A
      (restate scope, defer rotation proof to ~r335) recommended. Leo confirms.

NOTES / PARKED
- ~r335 (production UI): parked until this gate accepts. It will implement only
  what Leo accepted, nothing else.
- Brand copy + the 50-fact localized corpus: folded into this acceptance —
  reviewed during the walkthrough, no separate row.
- Contract ownership gap: the contract is drafted but no Shadow row owns it yet;
  give it a row at or before acceptance.
- Provenance: verified 2026-09-20 against the r334 clean snapshot (source
  8ab5fc18, review state pending_leo_walkthrough). Read-only pass — no package,
  Shadow, or ~r335 changes. All contrast numbers computed this session from the
  files, not recalled."""

R334_SECTIONS = {
    "STATUS": (
        "Corrections on the ~r334 scan-wait review package are confirmed and fix-ready;\n"
        "waiting on Leo's acceptance walkthrough. Nothing else blocks."
    ),
    "BLOCKED ON / GATE": (
        "Leo's approval — one ~8-minute in-chat walkthrough of the review package\n"
        "(contract, prototype, state art). Leo says \"accept\" or names corrections per\n"
        "state; ~r335 stays parked until then."
    ),
    "NEXT ACTION": (
        "Leo: open the package and run the 8-step walkthrough; accept or dictate\n"
        "corrections per state."
    ),
    "KEY LINKS": [
        "[Review package (contract, prototype, state art)] → docs/design/review-center/scan-wait-r1/\n"
        "  (inside the r334 clean snapshot ~/.shadow/clean/resplit-r334-20260920-final)",
        "[Corrections checklist — 8 items, each with its fix] → r334-corrections.md",
        "[Prototype (self-contained, no server)] → prototype.html (same folder)",
        "[Shadow row] → ~r334 on Shadow plan entity 9c83f1f4",
    ],
    "CORRECTIONS CHECKLIST": [
        "1. Spanish title collision — \"start\" and \"completion\" screens share one\n"
        "      Spanish title/VO line. Fix drafted: start becomes \"Listo para empezar.\"",
        "2. SVG muted-text contrast — 5 state images fail 4.5:1 (#617276 on paper\n"
        "      = 4.35). Fix drafted: swap to #596a6d (passes everywhere).",
        "3. SVG gold contrast — slow-stalled only: #a66b13 fails on paper (3.84).\n"
        "      Fix drafted: #8a5c0e (passes, keeps the warn-gold look).",
        "4. \"Still working.\" honesty — the screen can't observe the provider\n"
        "      working. Proposed: \"Still waiting.\" Leo picks the final wording.",
        "5. Done button under 44pt — 38px today. Fix drafted: 44px, one line of CSS.",
        "6. Transition map gaps — 4 Close→ edges missing from the contract map.\n"
        "      Fix drafted: append the 5-line map block.",
        "7. Dark-theme review gap — colors pass; dark is only reachable on 2 of 7\n"
        "      states. Option A (Theme selector, small code) vs B (checklist note). Leo calls it.",
        "8. Fact rotation untestable — prototype shows one static fact. Option A\n"
        "      (restate scope, defer rotation proof to ~r335) recommended. Leo confirms.",
    ],
    "NOTES / PARKED": [
        "~r335 (production UI): parked until this gate accepts. It will implement only\n"
        "  what Leo accepted, nothing else.",
        "Brand copy + the 50-fact localized corpus: folded into this acceptance —\n"
        "  reviewed during the walkthrough, no separate row.",
        "Contract ownership gap: the contract is drafted but no Shadow row owns it yet;\n"
        "  give it a row at or before acceptance.",
        "Provenance: verified 2026-09-20 against the r334 clean snapshot (source\n"
        "  8ab5fc18, review state pending_leo_walkthrough). Read-only pass — no package,\n"
        "  Shadow, or ~r335 changes. All contrast numbers computed this session from the\n"
        "  files, not recalled.",
    ],
}


def test_golden_r334_end_to_end():
    composed = compose_note(R334_SECTIONS)
    assert composed == GOLDEN_AFTER  # composer alone reproduces the canonical note
    assert apply_render_guards(GOLDEN_AFTER) == GOLDEN_AFTER  # guards are no-ops on conforming text
    assert apply_render_guards(composed) == GOLDEN_AFTER  # full pipeline stays exact


def test_sections_present_empty_honest():
    note = compose_note({})
    headers = [line for line in note.splitlines() if line in SECTION_ORDER]
    assert headers == list(SECTION_ORDER)  # all six, template order
    assert note.count("Not blocked.") == 1
    assert note.count("None.") == 5


def test_compose_rejects_unknown_sections():
    with pytest.raises(ValueError):
        compose_note({"STATUS": "ok", "SUMMARY": "x"})


def test_no_full_hash_in_prose():
    before = (
        "Shadow entity 9c83f1f418d2ef8350d99ef94ee3b95cea2cd1538eabf61ee705224994ab9927, row ~r334."
    )
    guarded = apply_render_guards(before)
    assert "9c83f1f418" not in guarded
    assert "Shadow entity 9c83f1f4" in guarded  # label prose preserved, id truncated to 8
    assert "row ~r334" in guarded  # sentence already carries "Shadow entity" — compliant as-is


def test_row_expansion_skips_scorer_compliant_text():
    text = "Documented on the Shadow plan (~r334) review."
    assert apply_render_guards(text) == text  # paren gloss = expanded, per rubric §3a


def test_row_expands_first_occurrence_even_with_plan_mention_elsewhere():
    # A "Shadow plan" mention in an EARLIER sentence does not satisfy the
    # first row-token sentence — the scorer still demands expansion there.
    text = "Context for the Shadow plan. Row ~r334 awaits review."
    guarded = apply_render_guards(text)
    assert "~r334 (a Shadow plan row)" in guarded  # first occurrence expanded
    assert "Context for the Shadow plan." in guarded  # earlier prose untouched


def test_label_paths_bare_absolute():
    text = "Checklist: /Users/leokwan/.hermes/cache/scratch/r334-corrections.md for the 8 items."
    guarded = apply_render_guards(text)
    assert "[r334-corrections.md] → /Users/leokwan/.hermes/cache/scratch/r334-corrections.md" in guarded
    # Labeled-link targets and parenthesised paths are left alone.
    labeled = "[x] → docs/design/review-center/scan-wait-r1/\n  (inside ~/.shadow/clean/snap)"
    assert apply_render_guards(labeled) == labeled


def test_task_id_expansion():
    bare = "Blocked behind t_fbd28a28 on the board."
    guarded = apply_render_guards(bare)
    assert "t_fbd28a28 (a kanban task)" in guarded
    assert guarded.count("(a kanban task)") == 1  # first occurrence only

    # `card` does not satisfy the rubric's expansion — gloss still lands.
    carded = "context for (card t_fb55c86f) on the board."
    assert "(card t_fb55c86f) (a kanban task)" in apply_render_guards(carded)

    # The word `task` in the first id sentence IS the expansion.
    worded = "Open task t_fbd28a28 and finish it."
    assert apply_render_guards(worded) == worded

    # An existing paren gloss is left alone.
    glossed = "See t_fbd28a28 (a kanban task) first."
    assert apply_render_guards(glossed) == glossed


def test_paren_paths_labeled_routes_exempt():
    # A REAL file path inside prose parentheses is labeled (D decision, t_ca4e2742).
    real = "Gate after the walkthrough (/Users/leokwan/.hermes/cache/scratch/r334-corrections.md)."
    guarded = apply_render_guards(real)
    assert "[r334-corrections.md] → /Users/leokwan/.hermes/cache/scratch/r334-corrections.md" in guarded

    # API routes and slash-commands are not file references — untouched.
    routes = "Confirm via GET /v1/models and run /amplify; docs/design/x is relative."
    assert apply_render_guards(routes) == routes

    # A continuation paren group on a labeled line keeps its inner paths verbatim.
    continuation = "[x] → docs/design/scan-wait-r1/\n  (inside ~/.shadow/clean/snap)"
    assert apply_render_guards(continuation) == continuation


def test_recompose_freehand_to_template():
    freehand = (
        "Goal: unblock the ~r334 review lane (card t_fb55c86f), which is waiting on the "
        "walkthrough. Steps: perform the scan-wait walkthrough and fill out the corrections "
        "checklist linked from the card. Acceptance: the checklist is completed and posted "
        "back to t_fb55c86f, ready to fold into the review.\n"
        "Checklist: /Users/leokwan/.hermes/cache/scratch/r334-corrections.md"
    )
    out = apply_render_guards(freehand)
    headers = [line for line in out.splitlines() if line in SECTION_ORDER]
    assert headers == list(SECTION_ORDER)  # all six, in order, nothing before STATUS
    assert "Ready to start." in out  # first action is imperative → honest ready state
    assert "Perform the scan-wait walkthrough" in out  # own words (prefix-stripped, capped)
    assert "which is waiting on the walkthrough" in out  # gate state kept
    assert "(card t_fb55c86f) (a kanban task)" in out  # gloss after the paren group, no nesting
    assert "r334-corrections.md] →" in out  # path labeling still runs


def test_recompose_leaves_short_and_structured_text_alone():
    one_liner = "Run the gauntlet on the fresh 2.0.4 stamp, then upload."
    assert apply_render_guards(one_liner) == one_liner  # comment-length: no filler templating
    structured = GOLDEN_AFTER
    assert apply_render_guards(structured) == structured  # already carries the template


def test_strip_chrome():
    pasted = (
        "Real status line.\n"
        "\n"
        "ESTIMATE\n"
        "Estimate effort\n"
        "makes a model call\n"
        "COMMENTS · 0\n"
        "Comment\n"
        "ACTIVITY · 2\n"
        "created in Blocked\n"
        "7 min. ago\n"
        "blocked — needs human input\n"
        "initial_status\n"
        "ATTACHMENTS · 0\n"
        "No attachments yet.\n"
    )
    assert strip_board_chrome(pasted) == "Real status line."


def test_dedup_initial_status_double_stamp():
    # The real pair from task_events: created[status=blocked] + blocked[reason=initial_status]
    # at the same second; a genuine later block must survive.
    ts = 1789930821
    events = [
        {"id": 98, "kind": "created", "payload": {"status": "blocked"}, "created_at": ts},
        {
            "id": 99,
            "kind": "blocked",
            "payload": {"reason": "initial_status", "status": "blocked", "actor": "user"},
            "created_at": ts,
        },
        {"id": 100, "kind": "blocked", "payload": {"reason": "walkthrough"}, "created_at": ts + 60},
    ]
    kept = dedup_activity_events(events)
    assert [e["id"] for e in kept] == [98, 100]


def test_dedup_keeps_genuine_blocks_and_parses_string_payloads():
    ts = 1789930821
    events = [
        {"id": 1, "kind": "created", "payload": '{"status": "blocked"}', "created_at": ts},
        {"id": 2, "kind": "blocked", "payload": '{"reason": "initial_status"}', "created_at": ts},
        {"id": 3, "kind": "blocked", "payload": None, "created_at": ts},
        {"id": 4, "kind": "blocked", "payload": '{"reason": "initial_status"}', "created_at": ts + 5},
    ]
    kept = dedup_activity_events(events)
    assert [e["id"] for e in kept] == [1, 3, 4]  # twin dropped; unparsed + other-second stay
