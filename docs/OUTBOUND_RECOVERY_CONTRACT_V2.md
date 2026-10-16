# Durable outbound, receipts and cancellation recovery v2 — fixed contract

Baseline: PR2 `e5332720fe301285044781ab7020d223a0958b24`, PR1
`d4104b70660976c6d59fe8915a75ac5616bdecd3`, main
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`. This contract is recorded before
implementation/experiments. No local AGENTS.md or .agents skills were present.
Existing APIs, permanent quarantine and prior documents/evidence stay intact.

## Bounded next subset

Add an explicitly separate `OutboundRecoverySessionV2` for offline simulation.
An exclusively created directory binds account, session and strategy strings.
A retained, separately opened POSIX flock file excludes cooperating local
writers for that directory for the entire object lifetime. Every method checks
PID ownership; forked authority is invalid. This lock is neither account-wide
identity registration nor venue fencing. Another directory or a direct venue
caller can bypass it. No generation token is claimed to revoke in-flight work.

Only the process that explicitly creates a new directory may submit new orders.
Persist each exact new intent and actual dispatch timestamp before entering the
sink. Every reserved new is possibly sent, including a pre-sink crash. Never
retry new orders. Reopen is always cancellation/evidence-only; no rearm or
strategy restoration. New entry stays disabled even when flat/complete.
Every possibly sent order becomes cancellation work automatically on recovery.
Use client IDs exclusively for cancel addressing, stable within this journal.
Persist each cancellation attempt before sink entry. Each explicit recovery
step sends at most one cancel for an unresolved order. Repeated cancel requests
are allowed: cancellation is a request to stop execution, never exactly-once.
A cancel transport return is no evidence of venue cancellation. Retry bound:
four attempts per order, 64 orders, 256 total outbound reservations. Exhaustion
retains uncertainty and does not erase work. Caller controls retry timing; no
unbounded retry loop, reconnect or background scheduler is introduced.

Capture validated immutable receipts into the same atomic journal before
returning success to the delivery adapter. The adapter must retain/redeliver
until that local acknowledgment; this cannot ensure capture if the adapter or
venue loses a receipt. Kill before receipt commit permits redelivery; kill
after commit deduplicates redelivery and fills. Conflicting receipt/report/fill
identities fail closed. Preserve actual delivery order while accepting delayed
and reordered exchange evidence. Record raw receipts and rebuild/verify the
fold on every reopen; no separately committed cursor can skip a receipt.

Use the existing strict durable evidence fold, supplemented by actual dispatch
causality and exact intent/receipt binding. An absent snapshot, unknown-order
cancel rejection, missing acceptance or transport success never proves a new
request will not arrive later. Only positive, causally valid immutable terminal
evidence for that exact client/venue identity ends cancellation work. Missing
fill detail keeps inventory/accounting uncertain even after cancellation work
ends. No open-order snapshot absence or time deadline closes a reservation.
An order terminal at a venue that permits ID reuse or two venue orders for one
client ID is outside this contract: conflicting observed identities refuse
recovery, and unobserved duplicate effects remain unbounded by this journal.

Journal limits: 64 orders, 256 outbound records, 1024 receipts, 4 MiB; strict
version/context/checksum/schema/count checks. Atomic replace + file/directory
fsync on a retained local POSIX filesystem. Any uncertain write, sink failure,
invalid incoming evidence or reentrancy poisons the object; fresh verified open
is necessary. No hostile tamper, rollback, power loss or arbitrary filesystem
claim. Receipt acknowledgment means local durability under these assumptions,
not broker settlement. Scope is saved orders with zero starting inventory;
account-wide completeness and authoritative starting balances are absent.

## Failure matrix fixed before trials

| Boundary/fault | Independent observation | Required result |
|---|---|---|
| New reserve at partial write, file fsync, replace, directory fsync, before/inside/after sink | Surviving venue sees zero or one initial new | Reopen emits no new; reserved but unseen stays unresolved |
| Accepted new, lost acknowledgment | Venue accepts while child dies | Cancel recovery stops remaining executable quantity; never resend new |
| Cancel reserve/send/return crash | Repeated venue cancellations and delayed first cancel | Retry safe as cancellation request; no false terminal proof |
| Receipt partial write/replace/fsync/return death | Disk-only reopen plus duplicate delivery | Old/new complete prefix only; ledger fills booked once |
| Unknown cancel before delayed new | Venue retains old request across client death | Unknown stays open; later new can fill until another cancel |
| Lost/reordered/duplicate reports and fill detail | Integer venue quantities and separate scalar ledger | Preserve conservative interval, exact unique fill cash and costs |
| Terminal status without fill detail | Venue quantity versus received fill ledger | Stop cancel work; report missing accounting evidence |
| No terminal receipt, retry exhaustion | Venue may be cancelled or live | Remain unresolved and entry disabled |
| Competing same-directory process / inherited object | Independent subprocess lock attempt | Refuse local second writer; release after real death |
| Separate directory / delayed old-process effect | Independent account venue trace | Demonstrate bypass; never claim account fencing |

## Invariants, baselines and stopping criteria

For every prefix: no new dispatch after reopen; venue fills per stable saved
identity never exceed its reserved size under simulator identity semantics;
original = fills + residual at venue; unique detail inventory and integer tick
cash agree with independent signed scalars; authoritative inventory lies in the
received-evidence interval. Receipt notification never predates exchange or
reserved dispatch. Neither unknown nor absence shrinks the full uncertainty.
Cancels remove only residual; fees/rebates book once. A terminal report may
narrow execution quantity without supplying enough detail to book it.

Compare the old permanent-quarantine baseline (six fills when cancel work is
lost; two when recovered before the second trade) on the same surviving venue.
Also retain the delayed-new counterexample where an early unknown cancel cannot
prevent later fills. Real SIGKILL/fresh interpreters and disk-only handoff must
be used, not an in-memory copy called a restart. Snapshot venue inputs/effects,
client journal, receipts and prefix audits; exclude PIDs/wall time from stable
artifact comparisons. Repeat deterministically in one dependency environment.

An independent reviewer authors separate adversarial tests and quantity/cash
oracles without importing the primary harness oracle. Preserve before/after
witnesses for demonstrated implementation defects, fix blockers, repeat review.
Stop only after scoped implementation, real deaths, independent review,
aggregate checks, old evidence preservation, reproducibility and exact-head CI
reach terminal outcomes. If a stronger property is disproved, retain fail-closed
uncertainty and publish the counterexample. No calibrated-market, profitability,
production-readiness or exactly-once venue-effects claims.

## Pre-trial clarification from independent design review

V2 rejects fills outside the saved limit price (price improvement is allowed),
and terminal reconciliation state without a venue identity bound by a prior
report or a fill in that response. A terminal report carries that identity
even when execution detail is absent. The local lock releases by descriptor
close, never explicit unlock through a forked descriptor. An inherited open
descriptor can delay lock release after parent death; this is an availability
failure that continues to exclude writers, not a fencing guarantee.
