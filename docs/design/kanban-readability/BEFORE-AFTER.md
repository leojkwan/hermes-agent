# Kanban / Shadow note readability — before & after

Pipeline: `readability-render-v3` (`hermes_cli/kanban_readability.py`,
`hermes_cli/kanban_output_tracing.py`). Rendered by
`apply_render_guards()` (which calls `recompose_note()` first).

This is the *actual note* that triggered the work, rendered through the
current implementation — not a mockup.

## BEFORE (verbatim)

> Shadow entity 9c83f1f418d2ef8350d99ef94ee3b95cea2cd1538eabf61ee705224994ab9927,
> row ~r334. Gate: Leo approval after walkthrough of
> docs/design/review-center/scan-wait-r1/ (contract, prototype, state images).
> Corrections checklist: /Users/leokwan/.hermes/cache/scratch/r334-corrections.md
> (Spanish title collision, SVG contrast tokens, Still-working honesty wording,
> 44pt Done target, transition map, dark-theme reviewability, fact rotation).
> ~r335 production UI stays parked until acceptance. Brand and the 50-fact
> localized corpus fold into this acceptance; the contract is drafted, no owned
> row yet.

Failure modes: one run-on paragraph; a 64-character raw SHA; unexplained tokens
(`~r334`, `Gate:`, `parked`); raw absolute filesystem paths inline; a seven-item
checklist buried inside a parenthetical; no single imperative next action.

## AFTER (render v3, commit a33fc92d28)

```
STATUS
Blocked — awaiting gate.

BLOCKED ON / GATE
Gate: Leo approval after walkthrough of docs/design/review-center/scan-wait-r1/ (contract, prototype, state images).

NEXT ACTION
Blocked — see BLOCKED ON / GATE.

KEY LINKS
Corrections checklist: [r334-corrections.md] → /Users/leokwan/.hermes/cache/scratch/r334-corrections.md.

CORRECTIONS CHECKLIST
- [ ] Spanish title collision
- [ ] SVG contrast tokens
- [ ] Still-working honesty wording
- [ ] 44pt Done target
- [ ] transition map
- [ ] dark-theme reviewability
- [ ] fact rotation

NOTES / PARKED
- Shadow entity 9c83f1f4, row ~r334.
- ~r335 production UI stays parked until acceptance.
- Brand and the 50-fact localized corpus fold into this acceptance.
- The contract is drafted, no owned row yet.
```

Render sha256[:16] `cb73038e92124989`; `f(f(x)) == f(x)` verified; the gate
line's comma-paren survives intact.

## What demonstrably improved

| Failure mode | Before | After (v3) |
|---|---|---|
| Raw 64-char SHA | `9c83f1f418d2…4ab9927` | `9c83f1f4` (truncated at the read boundary) |
| Single run-on paragraph | 1 block, 6 sentences | 6 labeled sections, fixed order |
| Gate buried mid-paragraph | inline | own `BLOCKED ON / GATE` section, comma-paren intact |
| **Checklist buried in a paren** | 7 items welded to a path in prose | own `CORRECTIONS CHECKLIST`, 7 checkable `- [ ]` items |
| **STATUS dishonesty** | n/a (structure only) | gate-parked work reads `Blocked — awaiting gate.`, not `In progress.` |
| **NEXT ACTION blank** | n/a (structure only) | `Blocked — see BLOCKED ON / GATE.` pointer, never a fabricated imperative |
| Bare filesystem path | inline prose | `[r334-corrections.md] → /full/path` labeled shape |
| Mid-clause fragments | trailing `;` kept | terminator cleaned (`…acceptance.`) |

Design rules held: the recomposition layer routes the author's own words into
sections — it never paraphrases and never invents. Checklist items are verbatim
substrings; STATUS/NEXT ACTION are derived from where content actually routed,
not assumed. The checklist splitter is keyed on the checklist's own LABEL,
never on "a paren holding commas" — that is why the gate line's
`(contract, prototype, state images)` is untouched.

## Verification

- 79/79 green: `test_kanban_readability.py` (21 tests, 7 new invariants incl.
  the gate-paren negative test), `test_kanban_output_tracing.py`,
  `test_kanban_dashboard_plugin.py`.
- Suite repair lane at `8aeea131e1` (pre-v3): full-suite pass 11 = 54,225 pass
  / 1 fail (that fix landed post-launch; green ×3 isolation + green in the
  26-file `-j8` flake set, EXIT=0).

## Remaining scope limit (honest)

Only one production caller routes through this pipeline today: the kanban
dashboard plugin's note emission (`kanban_output_tracing.py`). Shadow's own
skill notes and other surfaces do not call `apply_render_guards`. "The entire
output system" is therefore kanban-note output today; widening the surface is a
separate, deliberate wiring decision per surface, not a render change.
