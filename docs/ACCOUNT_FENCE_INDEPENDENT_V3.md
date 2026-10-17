# Independent adversarial review of the simulated account fence v3

This review begins against PR2 baseline
`3d261672ec4dc8d55e2c01f59c283e4f85de4bb2`, before the new boundary's
experiments. All inputs are synthetic and uncalibrated. The reviewer authors
separate tests and scalar quantity/cash expectations without the primary
harness's oracle. Existing permanent quarantine and v2 APIs remain separate.

## Pre-experiment model challenge

A SQLite transaction can represent an authoritative synthetic venue only when
it contains both the current account generation check and the resulting venue
effect. A local writer lock, a client-side check before enqueue, or a token
checked before releasing a transaction cannot revoke a delayed command. Every
new/cancel command must carry a token checked at processing. Ownership change
must advance the retained account generation and irreversibly disable entry in
the same transaction. Repeating an owner or session string must never recover
an earlier generation's authority.

Orders accepted before that change remain exposure. A fence cannot undo their
fills or turn an ownership acknowledgment into cancellation evidence. The
independent expectation for a seven-unit bid at 113 is inventory +2 and trade
cash -226 after two units fill; an ask mirrors both signs. Cancelling the
remaining five then prevents later venue fills under the modeled venue's
cancellation semantics. Lost terminal delivery may leave client knowledge
uncertain despite that authoritative cancellation. Exhausted cancellation
attempts cannot erase live residual quantity or prove a terminal result.

Retained account identity must cover competing clients using separate local
journals, strategy names and reused owner strings. Client/order/fill identities
must retain tombstones after cancellation or completion. A command retry must
not create an additional order by reusing the same identity. Any immutable ID
reused with conflicting payload must fail closed. Evidence and cancellations
must remain scoped to the correct account and immutable order identity.

The process experiments must expose actual competing interpreters and record a
transactional event sequence. A final state alone cannot show whether a stale
effect occurred before or after takeover. Death after commit but before reply
must leave a retained effect, without implying that retrying a request produces
exactly one transport or venue action. Independent scalar quantities and cash
must be derived from unique venue fill identities, separate from received
client detail and quantity bounds.

## Required counterexamples and exclusions

- Restoring an old authoritative database can restore an old generation and
  entry permission. The model has no independent anti-rollback watermark.
- A second authority database, bypass command path, or alternate name for the
  same external account is outside this single-authority exact-string model.
- A command accepted before ownership changes can fill afterward until a
  separately committed cancellation removes its executable residual.
- Lost receipts and finite retry budgets can retain uncertainty indefinitely.
  Receipt absence and a successful transport return are not terminal evidence.
- Tokens describe trusted issuance and coordination among simulated callers;
  generation values are not cryptographic authorization against an attacker.
- Two processes sharing the same current token cannot be distinguished. The
  fence identifies an owner context, not exactly one operating-system process.
- Process SIGKILL is not storage power loss; SQLite durability assumptions are
  bounded to the tested retained local filesystem and transaction configuration.

## Tested outcomes and implementation defect

The independently authored tests use the public API in
`tests/integration/test_account_fence_independent.py` and a separate interpreter
worker in `tools/account_fence_independent_worker.py`.

- Two already-live interpreters race generation-zero claims. Exactly one
  succeeds; the loser can subsequently take over generation one. The old
  process's new and cancel commands are rejected after that change, and entry
  is disabled for the current owner too.
- Real SIGKILL before and after transaction commit is exercised separately for
  ownership and accepted new commands. Reopen sees the complete old or complete
  committed state. A committed accepted order remains discoverable by command
  identity even when the requesting process never receives the return.
- In both bid and ask cases, an old accepted seven-unit order fills two units
  before takeover and one afterward. Current-owner cancellation removes the
  remaining four. Independently summed raw unique fills produce inventory
  `+3/-3` and integer trade cash `-339/+339`. Terminal delivery before either
  fill detail yields exact quantity bounds while the received-detail ledger
  remains zero. Delayed reverse-order detail and duplicate delivery then book
  each fill once and agree with those signed scalars.
- Retained owner reuse and client identity reuse after cancellation are refused.
  Conflicting command payload and wrong-authority token attempts leave the
  retained state unchanged.
- Four cancellation requests lost before venue processing exhaust the local
  retry budget. The client keeps `[0,7]` uncertainty and disabled entry; all
  seven pre-existing units can still execute, with trade cash `-791`.

Independent review found a source-binding defect in the initial implementation
pinned at `00d22c4`. A genuine cancellation receipt from another modeled account
could share a client identity. The original conversion into v2 evidence dropped
the account and authority fields, so a client with a lost acceptance could mark
its own order terminal and suppress cancellation. The unchanged probe in
`tools/account_fence_identity_witness.py` produces seven avoidable fills and
`-791` trade cash on that pinned source. The corrected v3 adapter's source-bound
`capture` rejects the foreign terminal, allows the own cancellation, and yields
zero later fills and zero trade cash. It also rejects altered own reports and
accepts genuine delayed fill detail for a prior generation's order. Bare
`to_receipt` plus direct v2 capture remains an explicitly untrusted bypass of
the v3 adapter; no existing v2 API or evidence format was silently strengthened.

The independent counterexamples also execute, rather than merely assert, the
model's limits. Two live processes given the identical current token both submit
different seven-unit orders: fourteen units can execute, with trade cash `-1582`.
Restoring a prior authoritative SQLite backup resurrects generation one and its
entry permission: the revoked old token then submits seven units that execute
for trade cash `-791`. Neither outcome contradicts the narrowly scoped token
fence, and both defeat stronger process-exclusivity or anti-rollback claims.

Actual race inputs, replies and ordered authority states are retained in
`independent/runtime-races.jsonl`. Their order depends on scheduling, so this
JSONL trace is separate from deterministic `.json` outcome witnesses and their
manifest. Stable race summaries record invariant outcomes; they are not
substitutes for the retained chronology.

No live safety, calibrated market performance, or exactly-once venue-effects
conclusion follows from these experiments.
