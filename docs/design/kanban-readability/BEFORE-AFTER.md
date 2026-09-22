# Kanban / Shadow note readability — before & after

Pipeline: `readability-render-v2` (`hermes_cli/kanban_readability.py`,
`hermes_cli/kanban_output_tracing.py`). Rendered by
`recompose_note()` → `apply_render_guards()`.

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

## AFTER (current HEAD)

```
STATUS
In progress.

BLOCKED ON / GATE
Gate: Leo approval after walkthrough of docs/design/review-center/scan-wait-r1/ (contract, prototype, state images).

NEXT ACTION
None.

KEY LINKS
Corrections checklist: [r334-corrections.md] → /Users/leokwan/.hermes/cache/scratch/r334-corrections.md (Spanish title collision, SVG contrast tokens, Still-working honesty wording, 44pt Done target, transition map, dark-theme reviewability, fact rotation).

CORRECTIONS CHECKLIST
None.

NOTES / PARKED
- Shadow entity 9c83f1f4, row ~r334.
- ~r335 production UI stays parked until acceptance.
- Brand and the 50-fact localized corpus fold into this acceptance;
- The contract is drafted, no owned row yet.
```

## What demonstrably improved

| Failure mode | Before | After |
|---|---|---|
| Raw 64-char SHA | `9c83f1f418d2…4ab9927` | `9c83f1f4` (truncated at the read boundary) |
| Single run-on paragraph | 1 block, 6 sentences | 6 labeled sections, fixed order |
| Gate buried mid-paragraph | inline | own `BLOCKED ON / GATE` section |
| Bare filesystem path | inline prose | `[r334-corrections.md] → /full/path` labeled shape |
| Parked scope buried | inline | own `NOTES / PARKED` bullets |

Design rule held: the recomposition layer routes the author's own words into
sections. It never paraphrases and never invents content — so a section with no
source material renders its filler rather than a fabricated sentence.

## Known gaps (NOT yet fixed — for the verifier, card t_c037ca07)

1. **`CORRECTIONS CHECKLIST` renders `None.` while seven checklist items sit in
   `KEY LINKS`** as the same parenthetical run-on the card exists to eliminate.
   The items are attached to the path token, so they route with the link. This is
   the single most visible remaining defect: the section that should carry them is
   empty.
2. **`NEXT ACTION` renders `None.`** The note does imply one (run the walkthrough,
   then get approval), but it is stated as a gate, not an imperative. Correct per
   the no-invention rule, yet it means the highest-value line is blank.
3. **`~r334` is not glossed here.** The first-occurrence row gloss
   (`Shadow plan row (…)`) does not fire on this path.
4. Bullet 3 keeps a trailing `;` — the sentence splitter cuts mid-clause.

Gaps 1 and 2 mean this note is *structurally* far better but not yet "10x more
readable" on the two lines a human reads first.
