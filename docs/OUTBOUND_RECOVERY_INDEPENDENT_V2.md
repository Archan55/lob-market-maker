# Independent adversarial review of outbound recovery v2

The independent reviewer authored
`tests/integration/test_outbound_recovery_independent.py` without importing the
primary study's venue, fixtures, accounting oracle or verdict helpers. The
review covers the explicit `OutboundRecoverySessionV2` API and leaves the v1
permanent quarantine API unchanged. The fixed contract precedes implementation
at `f5242a6`; prior PR2 evidence remains the baseline.

## Completed checks and practical results

The independent suite has 87 passing cases, including ten actual client
SIGKILLs followed by fresh interpreters or verified disk-only opens. Five of
those cases use a separately surviving venue process implemented only with the
Python standard library and integer quantity/cash arithmetic. Four kill a
client after a single new request reaches that venue; one kills a cancellation
client after the venue has processed its request. Five further deaths occur at
receipt persistence/return boundaries. A separate clean-interpreter fork probe
checks inherited authority without forking the multithreaded test runner.

On the independent seven-unit order, the venue executes two units before the
recovery opportunity. Without later cancellation it executes another five;
with recovered cancellation it remains at two. Integer trade cash is therefore
-791 or -226 ticks at price 113. At each observed venue prefix, original seven
units equal fills plus residual. Exactly one new request reaches this venue.
These quantities differ intentionally from the primary six-unit baseline and
are calculated independently.

The delayed-new pair is the counterexample to treating an unknown cancel as
closure. The original client's queued new request remains at the venue after
that client dies. A fresh client's first cancellation receives `unknown_order`;
only afterward does the old new request arrive. Stopping recovery there allows
all seven units to execute. A second cancellation after arrival stops execution
at two. Unknown evidence retains the full [0, 7] inventory interval and
unresolved cancellation work; it supplies no terminal proof.

The cancellation-death case makes two real cancel requests. The first cancels
the remaining five units, but its client dies before receipt capture. Reopen
retains one unresolved attempt and issues the second request. A successful
transport return still leaves work unresolved until positive terminal evidence
is captured. The venue sees two cancels and one new; no single-effect venue
claim is made.

Receipt deaths occur after a partial write, file fsync, replace, directory
fsync and successful return. Disk-only recovery exposes either the old complete
prefix or the new complete prefix. Redelivering a two-unit fill and then a
five-unit fill books seven units once, -791 tick cash, 0.50 fees and 0.05 rebates.
BID and ASK tests separately check signed quantity and cash; decimal accounting
also survives a caller with precision four and a small exponent bound.

A mixed-side control buys two units and sells five at 113: inventory -3 and
cash +339. The independent received-evidence interval remains [-5, 2], and the
seven units of residual reservations stay visible. Cancellation terminal state
without fill detail stops cancellation work while leaving accounting incomplete
and new entry disabled. Four per-order attempts and the global 256-record cap
both retain uncertainty. The global cap can exhaust work after only three
attempts per order when 64 new reservations already consume capacity.

Other checks cover actual dispatch causality, both sides of the limit price,
exact client/venue/strategy binding, overfills, conflicting immutable identities,
terminal quantity and state contradictions, duplicate/reordered receipts,
invalid schema/context/version/checksum-bound content, 4 MiB and count limits,
storage failures, detached snapshots, local competing processes, fork ownership,
sink failures and swallowed reentrant access before or after persistence.

## Defects found and corrected during review

Three demonstrated defect classes were preserved before production changes:

1. A callback could swallow a reentrant-access exception, after which the outer
   operation cleared poison and returned success. The fix latches the violation
   and checks it before sink entry and successful completion. Three direct
   regression cases and fourteen pre-sink snapshot/close controls pass.
2. The immutable-receipt shortcut accepted a duplicate envelope with notification
   time eight even though actual dispatch was ten and exchange acceptance was
   eleven. The fix validates incoming causality before duplicate acknowledgment.
3. A live state at exchange time fifty could be accepted alongside cancellation
   at time forty for the same stable venue identity. The folded terminal result
   hid the observed revival. v2 now rejects nonterminal state after terminal
   evidence in either receipt order, and rejects incompatible terminal status
   kinds even when their execution quantities agree. Four report/query revival
   cases and two terminal-kind cases pass.

The unchanged initial implementation is pinned at
`c46febf6dcb4290d7c4faa4a0c1b3d01526eb189`; the source after the first two fixes
and before terminal-history checks is pinned at
`e68f1931bd9ef93c83ae4f50a996bb3408bafb4d`. Reproduce before/after outcomes with
`tools/reproduce_outbound_review_defects.py`. No failing expectation was removed
or weakened to obtain the final passing result.

## Retained evidence and reproducibility

Run the suite with an empty destination:

```sh
.venv/bin/python -m pytest tests/integration/test_outbound_recovery_independent.py \
  -W error --outbound-recovery-output /tmp/outbound-independent-repeat1 -q
.venv/bin/python -m pytest tests/integration/test_outbound_recovery_independent.py \
  -W error --outbound-recovery-output /tmp/outbound-independent-repeat2 -q
diff -r /tmp/outbound-independent-repeat1/independent \
  /tmp/outbound-independent-repeat2/independent
```

The reviewer completed two such runs with 87 passes each and a byte-identical
`independent/` tree, including the audit manifest. Ten stable named witnesses
retain raw durable journal envelopes, client evidence prefixes and independent
venue inputs/effects. `independent/audit.json` records ten process deaths,
artifact hashes and the independent test source hash. Ports, PIDs, wall time
and temporary paths do not enter these artifacts. The combined repeat verifier hashes these files separately from the primary
study's manifest.

## Explicit unresolved guarantees

The tested cancellation subset has no remaining identified blocker under the
fixed assumptions. This review does not establish account-wide order coverage,
authoritative starting balances, durable venue-side receipt retention, or
revocation of old in-flight requests. Another directory or direct venue caller
bypasses the cooperative local lock. An inherited lock descriptor may delay
release; rejecting forked authority does not establish venue fencing. Retries
can all be lost, terminal receipts can remain missing, or detail can be
incomplete after terminal status. Entry remains disabled in those cases.

Observed conflicting identities or revived state are rejected, but unobserved
client-ID reuse or duplicate venue effects are outside the bound. Receipt
redelivery depends on an adapter retaining receipts until local acknowledgment.
Hostile rollback, power-loss durability and arbitrary filesystem behavior are
outside this local POSIX process-death contract. The traces are uncalibrated
synthetic controls and imply neither profitability nor production readiness.
