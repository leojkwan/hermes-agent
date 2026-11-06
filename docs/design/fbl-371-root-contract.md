# FBL-371: scoped root contract

Design only, pinned to `565a2c226c7de079e0d97466789d6d0703ad330d`. Grounding: `fbl371-provenance-trace.md` and current source. Reuse the existing delivery/automation machinery. Finish requires four independently verified tasks delegating through one retained receiver, with unchanged loop controls passing. No implementation or green-test claim.

## 1. Issuance

Initially qualify only direct Telegram polling messages and Slack Socket Mode messages, admitted by their registered native adapter into the same multiplex gateway. Require an authorized, identified, explicitly non-bot platform principal, a live callback binding, and successful native admission. “Human” means that platform principal, not proof of physical typing. User-account automation and compromised runtime/OS access are outside this attestation.

At `plugins/platforms/telegram/adapter.py:7113`, capture proof that `from_user` exists and `is_bot is False`; the default-false construction at `plugins/platforms/telegram/adapter.py:7140` is insufficient. At `plugins/platforms/slack/adapter.py:4628`, require a successful, workspace-bound `users.info` result explicitly reporting non-bot, with no bot/app-origin markers; `plugins/platforms/slack/adapter.py:3382-3410` currently collapses lookup uncertainty to false, so introduce a separate positive attestation. Registered authenticated transports are at `plugins/platforms/telegram/adapter.py:1799` and `plugins/platforms/slack/adapter.py:1234`.

Bind this proof privately to the exact callback event/source at `gateway/session_ingress_context.py:79,203`; preserve it through freezing at `gateway/session_envelope.py:202,237`. Carry an internal proof object into the admission commit seam `gateway/session_authority.py:613-623`, after authorization/reconciliation. Reuse an existing admission's root before generating a random root for a new admission; never remint on restore. Deduplicate by authenticated native input identity; redelivery, edit, and `/retry` reuse the original root. Approval/clarification replies, steering continuations, machinery, and synthetic events never issue roots. Public fields, absent author, and `is_bot=False` never establish this proof. In particular, `session.submit` at `gateway/session_authority.py:717` does not qualify.

## 2. Representation and trust

Add a runtime-held root registry beside `agent/bot_chain.py:29`, owned by the gateway's `SessionAuthorities` instance (`gateway/session_authorities.py:27`). Scope it to one process epoch and its served profiles. It holds root, limit, spent credits, participant state, and reservations. No key/file secret, new service, or model-accessible issuance RPC.

Transport an opaque, random reservation handle, not an authoritative root/budget supplied by a client. Each record binds its root, authenticated sender/profile, recipients, message digest, allocated hop, and originating admission. `agent/turn_author.py:77` may preserve a bounded candidate handle; parsing grants nothing. Verify it against the registry and exact delivery at `gateway/session_bot.py:808,958`. A copied handle cannot authorize a changed sender/body/recipient; identical replay is idempotent. Unknown, forged, expired, foreign-epoch, or missing handles use today's session accounting; never create a registry entry from a claim or reinterpret a bot as human.

Remaining budget is `limit - spent` in the registry. Handles reference that live state; copied envelopes cannot restore an earlier balance. Expiry never recreates credit. Restart loses root privileges and falls back conservatively; durable cross-process roots are deferred. Immutable execution context carries either verified lineage or legacy status, plus the fallback accounting owner. Continuations missing that owner refuse sends rather than opening fresh child budgets.

## 3. Inheritance and timing

Reserve in `agent/bot_chain.py:149`; carry the internal reservation through `tools/bot_mode_dm_chain.py:84-87`. Finalize recipient-specific handles at local dispatch `tools/bot_mode_dm.py:363`, binding the rendered message, not its pre-format tool body. Pin them with the intent at `tools/bot_mode_dm.py:559-582`, recipient receipt at `gateway/session_bot.py:971`, and automation snapshot/restore at `gateway/session_automation.py:549,597`. The allowed retry at `gateway/session_bot.py:617-627` reuses the same root/reservation, without allocating credit again.

Bind a verified immutable lineage object in `gateway/session_ingress.py:325`; pass it separately from attribution at `gateway/run_turn_runner.py:992`, threading it through `agent/turn_facade.py:29` and `agent/conversation_loop.py:1658,1821`. Clear/rebind it at `agent/turn_context.py:1148`, before the chain hook at `agent/turn_context.py:1159`. This precedes identity binding at `agent/turn_context.py:1181`; neither `_current_task_id` nor delivery IDs identify the root.

Capture lineage when spawning, not when finishing: `tools/process_registry.py:543,1089`, watcher/event transport at `tools/process_registry.py:888` and `tools/terminal_tool_background.py:125`, then `gateway/run_notifications.py:161`. Snapshot async delegation at `tools/async_delegation.py:934` and emit it at `tools/async_delegation.py:1125`. A completion resumes its captured root, even after another human task; never borrow the latest native envelope (`gateway/session_automation.py:449`). An implicit returned bot reply debits once per waiter reservation; an already-charged explicit reply or replayed wake debits nothing. At exhaustion, deliver the result with exhausted lineage and prohibit further sends. Generic completions inherit without minting credit. Split batches by verified root, with a separate unverified bucket: TUI `tui_gateway/session_notifications.py:675`; gateway process/async keys `gateway/run_notifications.py:192-193,1505,1696`.

Capture before background-review scheduling at `agent/background_review.py:1374`, install at fork construction `agent/background_review.py:1000`, and carry through delegated child creation/execution at `tools/delegate_tool.py:299` and `tools/delegate_tool_child_run.py:936`. Fork creation spends nothing; child sends share the ledger. `_turn_origin` (`agent/bot_chain.py:77`) must not erase inherited lineage. Rotation keeps root identity and balance; migrate participant/session aliases with `agent/bot_chain.py:47,61` and `agent/compression_automation.py:7`, merging collisions without discarding debt/deduplication. Rebuilt agents recover lineage from their admitted input, never a session's latest root.

## 4. Accounting and fallback

For verified traffic, root accounting **replaces enforcement of the session hop cap**. Continue today's session accounting as shadow debt for fallback; enforcing that accumulated cap on verified roots would reproduce task four's failure. Missing/unverified traffic still enforces the original session cap and cooldown. Keep gateway ingress limits unchanged.

Atomically charge each admitted `message_agent` call one root credit, including the human kickoff; fan-out shares that charge. Refuse when `spent + 1 > limit`; monotonically constrain `limit` by participating profiles' `max_hops`. Bot receipt binds the already-charged reservation without charging again. At limit four, A→B→A→B→A consumes four sends; send five fails. Repeated valid low-hop handles cannot lower `spent`; forged handles encounter the unchanged session rule (`max(local+1, envelope)` at `agent/bot_chain.py:92`). Interleaved roots have independent ledgers. Preserve duplicate/no-reply algorithms (`agent/bot_chain.py:159-174`) per root/participant, and legacy behavior unchanged. Keep disabled-guard semantics. Refund only definite no-send reservations under the existing release conditions (`agent/bot_chain.py:126-136`); queued/ambiguous outcomes retain debt (`tools/bot_mode_dm_chain.py:71,112`).

Conservative paths: CLI/TUI/Desktop viewer submissions, finite/unattended/API/webhook/cron/plugin inputs, other adapters, peer HTTP/env delivery (`tools/bot_mode_dm.py:327`), Desktop relay (`tools/bot_mode_dm.py:336,348`; `tui_gateway/methods_bot_relay.py:79`), separate gateway processes, old receipts, and missing/ambiguous continuation snapshots. No wire-field upgrade grants privileges.

## 5. Tests and acceptance

New test names → files:

- `test_independent_roots_admitted`, `test_uncertain_delivery_retains_root_budget` → `tests/tools/test_bot_chain_roots.py`.
- `test_forged_and_repeated_low_hop_capped`, `test_a_b_a_shares_budget`, `test_rotation_preserves_root`, `test_missing_lineage_fails_closed`, `test_lineage_downgrade_keeps_shadow_debt`, `test_parallel_sends_share_root_limit` → `tests/agent/test_bot_chain_roots.py`.
- `test_verified_human_issuance_only`, `test_human_redelivery_edit_retry_reuses_root` → `tests/gateway/test_bot_root_issuance.py`.
- `test_retry_reuses_root` → `tests/gateway/test_session_bot_retry.py`.
- `test_completion_wake_keeps_spawning_root`, `test_mixed_root_batches_split` → `tests/tui_gateway/test_bot_root_completion.py`; exercise gateway notification restoration too.
- `test_gateway_mixed_roots_never_coalesce`, `test_exhausted_reply_delivers_without_new_credit` → `tests/gateway/test_bot_root_completion.py`.
- `test_fork_inherits_root`, `test_delegated_child_inherits_root` → `tests/agent/test_bot_root_children.py`.
- `test_legacy_peer_unchanged` → `tests/gateway/test_peer_dm_bot_chat_authority.py`.

The supplied fixture's authorless direct hook calls cannot securely mint roots. Port its producer/retained-consumer scenario through verified native issuance and real delivery/restore; retain all four-task queued assertions. Its raw four-bot low-hop variant must remain capped. Keep `tests/agent/test_bot_chain.py:61-68`, ping-pong, duplicate, and no-reply tests byte-unchanged. Run via `scripts/run_tests.sh` under temporary homes; preserve prior broader-suite environment failures as unresolved until rerun green. Require non-author verification before implementation acceptance.
