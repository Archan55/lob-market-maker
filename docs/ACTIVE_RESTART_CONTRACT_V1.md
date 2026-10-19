# Active strategy restart boundary v1: pre-experiment contract

Baseline: PR2 ad108ebaad2edca9b2997dab95800080d5fb6802, stacked on unchanged
PR1 d4104b70660976c6d59fe8915a75ac5616bdecd3. No AGENTS.md or local skill
instructions were present in this cloud checkout. Prior reports stay unchanged.

## Decision and invariants

Full active restart is unsupported. DurableClient recovers delivered evidence,
not strategy ID allocation, managed quotes, market history, rate/backoff, risk
state, pending cancels or scheduler/channel requests. Even complete terminal
fill evidence does not establish a complete account/order universe or restore
those fields. A fresh strategy can reuse IDs and quote as though flat.

Add a separate opt-in guarded strategy session for synthetic offline research.
Explicit create starts a new bounded single-writer session, exclusively at a new
path. Before calling any command sink, synchronously persist an immutable
command reservation. A reservation means potentially sent, never exchange
acknowledgment. Never dispatch an identical or conflicting new client ID twice;
never dispatch the same cancellation instruction twice. Canonical intent
payloads and a caller-supplied session identity bind the journal. At most 256
commands / 4 MiB, strict versioned JSON with accidental-corruption checksum.

Open verifies the complete journal, then permanently quarantines that session.
It must refuse strategy decisions and every dispatch (including cancellation,
pre-crash cached actions and external stop-like requests) before changing
strategy state or calling a sink. It has no rearm method. A new interpreter,
valid old journal, empty journal, complete/flat evidence or late reports cannot
remove quarantine. Missing/corrupt state never implicitly creates a session.
The boundary is opt-in; ordinary backtests retain their existing behavior.

Evidence assessment is read-only. It joins independently verified DurableClient
intents to every reserved new order exactly, rejects missing/extra/mismatched
intents, and separates potentially outstanding orders, missing execution detail,
known inventory and pending cancellation reservations. No cancel reservation is
proof of cancellation. Missing/excess intent evidence fails closed. The durable
inbox remains assumed immutable and complete for the supplied delivery history.
No live receipt recorder, scheduler or venue recovery is introduced.

After uncertain persistence or sink failure the writer is poisoned and cannot
continue; a fresh open can inspect but cannot emit. Same-directory POSIX atomic
replace/file fsync/directory fsync is inherited from the bounded durable path.
Single writer and complete mediation through the guarded API are assumptions;
no claim covers direct strategy/sink calls, concurrent writers, hostile file
rewrites, anti-rollback, host power loss or arbitrary storage.

## Hypotheses fixed before experiments

H1: SIGKILL before reservation replacement leaves an old journal and zero sink
calls; after replacement leaves the reserved command, even if dispatch never
occurred. A fresh process always emits zero commands.
H2: death after sink effect but before caller acknowledgment leaves ambiguous
execution; restart preserves the reservation without retry or duplicate ID.
H3: missing or delayed cancel/report work, unknown order rejection, partial or
absent queries and fills during downtime cannot rearm or erase uncertainty.
H4: full terminal/flat evidence improves the assessment but cannot restore lost
active state. Evidence cannot silently omit a reserved order, add an unrelated
order or change a reserved intent.
H5: the guarded path preserves all prior behavior/artifacts, and refuses invalid
or incompatible journals, duplicate commands, file/count limits and storage
errors without unsafe command emission.

## Independent evidence and finite matrix

Use actual subprocess death/reopen with disk-only handoff. Exercise all seven
persistence/caller boundaries plus the sink-effect/return ambiguity. Use an
independently observable synthetic venue file with integer quantity/cash and
stable command identities; never use the production gate's own verdict as its
oracle. Separate subprocesses reconstruct the strategy after death. Pair an
unguarded baseline witness with the gated path: ID reuse, flat inventory and
lost pending cancel must be observable before claiming the boundary addresses
them. Do not attribute these unsupported restart attempts to an existing live
restart feature.

An independent reviewer authors separate tests/fixtures without importing the
primary study oracle. Review unknown outstanding exposure, late/duplicate fills,
missing cancellation work, direct/cached dispatch bypass, evidence binding,
complete-but-unsafe resumption, corrupt state and repeated reopen. Preserve any
development defect witness before fixing it. Report finite synthetic outcomes
and unresolved cases, not exactly-once venue effects or live readiness.
