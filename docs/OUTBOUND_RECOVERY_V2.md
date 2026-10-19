# Durable outbound, receipt and cancellation recovery v2

> Synthetic process-death mechanics evidence. These tapes and integer venue
> controls are uncalibrated. No profitability, live-readiness or exactly-once
> venue-effects claim is supported.

The new explicit `OutboundRecoverySessionV2` recovers cancellation work after
client death while keeping new entry permanently disabled on reopen. On the
same six-unit surviving-venue witness, the unchanged v1 quarantine permits all
six units to fill; v2 cancellation recovery stops at two. This reduces future
execution in that finite witness, without flattening the two executed units or
recovering an active strategy.

The [contract and failure matrix](OUTBOUND_RECOVERY_CONTRACT_V2.md) were committed
at `f5242a6` before implementation. Baseline PR2 is
`e5332720fe301285044781ab7020d223a0958b24`; PR1 and main remain unchanged.
The [independent review](OUTBOUND_RECOVERY_INDEPENDENT_V2.md) uses separately
authored fixtures and a surviving standard-library integer venue. The original
`GuardedStrategySession` and every earlier report retain their old behavior.

## Implemented contract

Explicit creation binds a new directory to account/session/strategy strings.
Only its creator process can submit new orders. Each exact intent and actual
dispatch timestamp is atomically persisted before sink entry. A reservation
always means possibly sent; new orders are never retried. Every verified open,
including an empty journal, starts with entry disabled and no rearm method.

Every reserved new order is implicit cancellation work after recovery. One
explicit recovery step durably records one cancel before sink entry. Repeated
cancellation is intentional: transport return does not prove cancellation.
Only positive terminal evidence for a bound stable identity completes the work.
An unknown cancel or absent query cannot establish that a delayed new request
will never arrive. Four attempts per order and 256 outbound records bound
retrying; exhaustion leaves unresolved work visible. With 64 new reservations,
the global cap can exhaust before four attempts per order.

Validated receipts are atomically persisted before the capture call returns.
Reopen reconstructs the entire evidence fold from raw receipts; there is no
separate cursor that can skip a durable receipt. Receipt/report/fill identities
prevent duplicate local accounting and reject conflicts. Actual dispatch
causality, limit prices and immutable terminal history receive additional v2
checks. Delayed exchange evidence may arrive out of order; local delivery time
must remain causal. Invalid evidence is not acknowledged, poisons the object
and leaves the last valid durable prefix. The adapter must retain/redeliver
until acknowledgment; this implementation cannot recover receipts that were
lost outside that protocol.

A retained separate flock inode excludes cooperating local same-directory
writers for the object's lifetime. PID checks reject inherited authority.
Closing inherited descriptors never explicitly unlocks the parent's lock.
These are local coordination measures, with no venue revocation or global
account identity authority. Another directory or direct venue caller bypasses
them, and retained in-flight requests can act after their sender dies.

Bounds: 64 saved orders, 256 outbound records, 1024 receipts and 4 MiB. Writes
use same-directory replace plus file/directory fsync. Uncertain persistence or
sink failure requires close and fresh verified recovery-only open. These are
retained-file POSIX assumptions, tested with SIGKILL rather than host power
loss. Inventory and cash cover saved orders from a zero starting position;
`account_complete` and `account_fenced` remain false.

## Completed primary matrix

The primary study passes **63 scenarios, 59 actual SIGKILL deaths and 218
independently audited client/venue prefixes**. Children own the client and disk
journal; the parent's Exchange and delayed transport survive. Fresh interpreters
receive no client object or in-memory checkpoint. Every prefix derives unique
fill inventory, integer cash, fees, rebates, execution intervals and reservation
quantities from raw received evidence, independently of the production fold.
Venue order conservation and the authoritative saved-order inventory interval
are checked separately.

| Fault or boundary | Observed result |
|---|---|
| New and cancel at ten reserve/sink/return boundaries, both sides | Old/new complete durable prefix; zero new dispatch after reopen; cancellation work survives even when sink acknowledgment is lost |
| Seven receipt persistence/return deaths, both sides | Redelivery reconstructs complete prefix; fills and costs book once |
| Initial new accepted but acknowledgment lost | Recovery cancels four residual units after two fills; no new retry |
| Cancel already processed before death, receipt absent | Another cancel request is possible; positive terminal evidence resolves work, transport return alone does not |
| Unknown cancel before old queued new arrives | Full uncertainty remains; later request can fill until another cancellation |
| Terminal state without fill detail | Cancellation work ends, but missing accounting detail remains and entry stays disabled |
| All cancellation attempts lost | Retry exhaustion retains unresolved full order; venue can still fill six |
| Cancellation succeeded but terminal receipts lost | Client still retains uncertainty despite venue cancellation |
| Same-directory competing writer and actual owner death | Writer refused while owner holds flock; fresh process acquires after death |
| Same account, separate directory | Account can fill twelve units despite first journal's six-unit scope; no account-wide bound claimed |

For every death, `crash-boundary.json` saves the committed journal and unfinished
staging bytes with random filenames removed. Inputs, venue commands/effects,
raw journals, client evidence and prefix audits are retained. The primary
[JSON manifest](OUTBOUND_RECOVERY_V2.json) hashes **314 canonical artifacts**;
its SHA-256 is
`d5d3f20e0998cb5188ea78c5e8cb8effd9e11bec78808a29dfb7bc4402f75476`.

## Independent review and before/after witnesses

The separate suite passes **87 adversarial tests**, including **10 additional
SIGKILL cases**. Its independent seven-unit venue witness executes seven units
without recovered cancellation versus two with cancellation. A mixed-side
scalar witness buys two and sells five at 113: inventory -3 and cash +339 ticks,
with received-evidence interval [-5, 2]. Repeated receipt delivery books seven
units once, -791 tick cash, 0.50 fees and 0.05 rebates in the seven-unit bid case.

Three new-path development defects were demonstrated and fixed: swallowed
reentrancy cleared poison; duplicate handling acknowledged a noncausal envelope;
terminal folding concealed observed revival or conflicting terminal status.
Pinned source commits `c46febf` and `e68f193` reproduce **ten failures before /
ten passes after** using identical independent test bytes. The independent
suite also covers storage faults, exact schema/context/count/byte bounds,
limit-price violations, competing local writers, fork inheritance and missing
or contradictory evidence. Its final review identifies no remaining blocker
within the fixed assumptions; it does not expand those assumptions.

## Reproduction, preservation and validation

After installing `.[dev]`, use a fresh destination:

```sh
python -m pytest tests/integration/test_outbound_recovery_independent.py \
  tests/integration/test_outbound_recovery_v2.py -q -W error \
  --outbound-recovery-output experiments/outbound-recovery-v2
python tools/verify_outbound_recovery.py
python tools/reproduce_outbound_review_defects.py
python -m pytest --cov=lobmm --cov-report=term-missing
```

Two combined complete runs match **326 JSON artifacts byte for byte**, including
314 primary artifacts plus its audit, ten independent raw witnesses and the
[independent audit](OUTBOUND_RECOVERY_INDEPENDENT_V2.json). The previous active
study matches **328 JSON files including its audit** at exact `e533272`; all
**36 prior documentation files remain byte-identical**. The existing active
verifier separately matches 327 listed artifacts and preserves the 128-artifact
durable study. The six earlier studies match **11,708 artifacts** at pinned
`8391031`; there are no old-evidence corrections in this change. Exact bytes
are verified within one dependency environment; timing and random staging
filenames are excluded, while staging byte contents are retained canonically.

Local cloud Python 3.12.14 validation passed **1,500 tests with 92.14%
branch-aware package coverage**, Ruff lint/format and strict mypy. The 300-event
smoke replay and canonical nine-case queue audit passed; the canonical
Markdown/JSON reproduced byte for byte with the documented output filename.
All pinned before/after defect helpers passed.

CI runs the combined study inside aggregate coverage, both repeats, exact prior
head preservation, all older verification/regression helpers and the smoke and
canonical queue replay. It uploads the new raw/repeated/review evidence as
`outbound-recovery-v2-<exact-checkout-sha>` for 14 days. See PR2 for the final
published SHA and terminal exact-head CI result.

## Unresolved practical risks

Cancellation recovery can limit later fills; it cannot undo executions. An
unknown response does not fence an old request. Repeated cancels may all be lost,
terminal receipts may never arrive, accounting detail can remain missing, and
bounded retrying may stop without closure. All retain disabled new entry.
The caller must choose cancellation timing and handle unresolved work; there
is no background retry scheduler or active strategy reconstruction.

There is no account-wide identity fence, complete account order universe,
authoritative starting balance, broker connection, durable venue retention,
anti-rollback protection or power-loss proof. Unobserved client-ID reuse or
multiple venue identities for one reserved ID can invalidate the saved-order
bound. Observed conflicts are refused. A local file lock supplies neither
exactly-once effects nor revocation of delayed old-process commands. Those
stronger guarantees remain unsupported and the retained bypass/delayed-new
counterexamples show why.
