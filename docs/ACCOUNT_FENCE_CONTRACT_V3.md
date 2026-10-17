# Simulated account authority v3 — pre-experiment contract

Baseline PR2 `3d261672ec4dc8d55e2c01f59c283e4f85de4bb2`, stacked on unchanged
PR1 `d4104b70660976c6d59fe8915a75ac5616bdecd3`. No repository AGENTS.md or
.agents skills exist. Existing quarantine, APIs and all prior evidence stay
unchanged. This contract precedes implementation and experiments.

## Bounded model and defended boundary

Add a separate opt-in `SimulatedAccountAuthorityV3`, explicitly a synthetic
venue authority, plus a cancellation-only v2 adapter. Its retained SQLite file
is the entire authoritative universe. SQLite serializes transactions; within
one transaction the authority checks the current account generation and applies
the command effect. This atomic check at processing time is the modeled fence.
A filesystem lock or token checked only when a command is queued is insufficient.
The file is a simulator implementation of the authority, not a venue fencing
primitive that can be applied to a real broker.

An exact account string names one account at one authority. Initial claim is a
compare-and-swap from generation zero and may enable entry. Every subsequent
successful claim increments generation, retains owner/session/strategy history,
and permanently disables new entry for that account in the same transaction.
Contenders present the generation they observed; only one can win that version.
Owner identifiers cannot be reused, including A→B→A. Token fields bind authority,
account, generation, owner, session and strategy; tokens are trusted simulated
credentials, not secrets, authentication or access control. Every command checks
all fields at processing, including cancellation and duplicate requests.

Retain client identity tombstones across terminal state, ownership changes and
separate local journals. Reject reused new client IDs and conflicting immutable
command IDs. A current owner can cancel a prior owner's retained order. A stale
owner cannot cancel a current owner's order. Reopening a v2 journal never
re-enables entry; authoritative takeover also disables entry in a fresh journal.
No automatic active strategy restoration, rearm, flattening or retry scheduler.

The authority starts at zero inventory, accepts only explicitly modeled orders,
and exposes its complete retained universe. This completeness is an assumption,
not inferred from journal contents or snapshots. It retains positive command and
fill evidence independently of delivery. The client still needs durable capture;
a lost/delayed receipt is not a cancellation acknowledgment. v2's four attempts
per order and global cap remain; exhaustion retains uncertainty. Positive terminal
evidence can stop cancellation while missing detail leaves accounting incomplete.
Retries are cancellation requests and may repeat; no exactly-once effects claim.

Bounds: eight accounts; 64 owner claims, 64 orders, 256 distinct command IDs and
1024 effects per account. Full retained state at most 4 MiB. No expiry/ID garbage
collection. Limit violations fail closed and may prevent progress. Each operation
uses a new SQLite connection, BEGIN IMMEDIATE, a checksummed bounded state and
FULL synchronous rollback-journal commit on a retained local POSIX filesystem.
Death before commit rolls back; death after commit loses only the return. No
connection or client object survives process handoff. SQLite is the single
assumed linearization authority. No network, brokerage or live exchange exists.

## Faults, invariants and exposure oracles fixed before trials

Use genuinely concurrent fresh interpreters with barriers, not sequential calls
labeled contention. Kill real processes before/after authoritative commit and
at client capture boundaries where useful. Retain input commands, chronological
authority events, raw journals, receipts and every observed state prefix without
PIDs or wall time. The independent reviewer authors separate scalar quantity,
identity and signed cash oracles; it must not import the primary oracle.

For each account prefix: generations strictly advance once per successful CAS;
entry never becomes enabled after takeover; no command bearing stale context
has an order effect after the takeover event; every client identity names at
most one retained order; original quantity equals executed + remaining +
cancelled; integer signed fill inventory and cash equal sums of immutable fills.
Client received-evidence intervals must cover the authority's modeled saved
orders, and details book once under duplicate/reordered delivery. Account and
saved-order scopes must be reported separately. A valid fence does not cancel
pre-existing orders: they may fill after takeover until cancellation arrives.

Paired witnesses compare local-only v2 versus authority-checked processing with
an old new command held across takeover. Also distinguish a new accepted before
takeover (future fills still possible) from a queued new processed afterward
(rejected). Challenge simultaneous first claim, simultaneous takeover, old
cancels after ownership change, same identities in new roots, owner ABA,
duplicate/conflicting commands, lost/delayed receipts, all cancel requests lost,
terminal receipts lost and cancellation exhaustion. Use both sides and a mixed
book for signed quantity/cash checks.

## Explicit counterexamples and stopping criteria

A second authority DB, a caller bypassing the gate, or an account alias can
create effects outside this universe. Roll back only the local client: current
authority rejects old tokens but lost local detail may remain unknown. Roll back
the authority itself: a previously revoked token can become valid again and
new entry can reopen; no anti-rollback or distributed consensus claim is made.
Retain concrete traces for these failures. Delayed pre-takeover acceptance can
fill after takeover, and fencing cannot undo those fills or flatten inventory.
A killed new-return path is ambiguous until retained authority evidence is read.

Commit the initial implementation before independent review fixes so defects
have pinned source and unchanged before/after test witnesses. Fix supported
contract violations without weakening tests. Publish practical quantity outcomes,
limits and unresolved guarantees. Repeat the new evidence byte-for-byte in one
dependency environment, compare all prior documents and v2 evidence against the
exact baseline, run aggregate lint/format/mypy/coverage and existing verifiers.
Publish only on existing draft PR2 and verify exact-head CI to terminal outcome.
All scenarios are synthetic and uncalibrated; no live safety, profitability,
exactly-once, hostile-tamper, host power-loss or authority-rollback guarantee.

## Implementation and review clarifications

Tokens name a transferable owner context. Two processes with the identical
current token are indistinguishable to this authority and can both submit valid
new orders before takeover. No process exclusivity or account exposure limit is
promised. Reusing the same configured authority ID on a second database does not
federate those databases or produce a global identity. Account strings receive
no alias canonicalization. These stronger properties require separate authority
and issuance assumptions and have executable counterexamples.

The bounded venue injects explicit side/quantity trade budgets into FIFO retained
orders at their saved limit prices, with zero fees/rebates. It is separate from
the historical L2 exchange, with no price discovery or calibration claim. Full
raw event replay reconstructs authority state; source-bound v3 receipt capture
checks the exact retained rich report before translating into unchanged v2
receipt types. Bare conversion followed by direct v2 capture is outside that
source-bound adapter and cannot authenticate provenance. Old-generation genuine
fill evidence remains valid after takeover. Free-running races retain their
actual chronology separately as JSONL; invariant JSON summaries repeat exactly.
Controlled transaction contention retains complete deterministic chronological
JSON, including requests, responses and the order of authoritative effects.
