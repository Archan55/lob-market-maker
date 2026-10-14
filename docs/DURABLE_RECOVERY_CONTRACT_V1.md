# Durable client recovery v1: contract fixed before experiments

Source inspected: PR2 `8391031107b0f18c4c0176099f9098d4195ed88b`, based on
unchanged PR1 `d4104b70660976c6d59fe8915a75ac5616bdecd3`. Existing checkpoints
are volatile clones. This study adds a separate, opt-in offline client fold;
it does not restart the simulator, venue, scheduler, active strategy or stop
controller and emits no commands. Existing versioned evidence stays intact.

## Recovery contract

The input is a finite immutable JSON inbox, already stored before recovery:
one session identity, all previously sent order intents, and a receipt sequence
in actual client delivery order. Only execution reports and frozen
reconciliation responses enter the sequence. Each receipt includes its stable
envelope identity and notification timestamp. Limits: 256 intents, 4096
receipts, and 4 MiB per persisted file. A caller supplies the expected session
and SHA-256 of the exact inbox bytes. Recording a live inbox is outside scope.

A versioned JSON checkpoint binds the session, inbox digest, applied prefix
offset, prefix digest, client quantity bounds, terminal reservations, identity
maps, fill details, earliest detail notification times and derived exact ledger.
No executable serialization or venue state is stored. Offset means the number
of completely applied receipts. Fill economics, identity knowledge and offset
are committed together. Recovery verifies the checksum and schema, reconstructs
the inbox prefix and requires exact equality with checkpoint contents before
exposing knowledge. The next receipt is replayed only from that offset.

A single writer replaces the checkpoint atomically: exclusive temporary file,
write, file fsync, same-directory replace, directory fsync, then acknowledgment.
An existing target cannot be silently initialized over, and missing, corrupt,
incomplete, incompatible or mismatched committed state blocks recovery. A crash
before replace leaves the previous committed prefix; after replace it leaves
the new prefix in these process-death tests. Surviving temporary files are
ignored. If a storage error occurs, that instance is poisoned and must reopen;
it cannot acknowledge uncertain state. Concurrent writers are excluded.

Stable envelope, report, query and fill identities bind immutable payloads.
Identical copies are idempotent even at different receipt offsets; conflicting
reuse fails closed. Shared fill identity unifies original reports and query
detail. Unknown-order rejection does not prove non-execution. Older live,
absent or truncated responses cannot widen established bounds or resurrect
terminal reservations. Quantity inferred from terminal state does not invent
missing execution costs. Knowledge describes delivered evidence, not venue
truth or a guarantee that no later execution occurred.

Checksums detect accidents, not malicious rewriting. A valid old checkpoint
can replay its suffix; no independent anti-rollback watermark is promised.
The inbox is assumed complete for the supplied delivery history, not complete
for venue execution. Host power failure, disk/controller failure, arbitrary
filesystems, live receipt capture and simultaneous writers are not proven.

## Hypotheses and oracles

H1: after actual child-process death at a persistence boundary, a fresh process
recovers exactly one valid old/new prefix and replay converges with uninterrupted
execution. H2: duplicated/reordered evidence preserves fill economics and
monotone quantity bounds across restart. H3: invalid committed state exposes no
client knowledge. H4: offline opt-in work preserves all old research artifacts.

Primary oracle: independently sum signed fill quantities, integer trade cash
and Decimal fees/rebates over a payload-consistent fill-ID union at every prefix;
intersect sent-quantity/state intervals and retain terminal status. Compare
known fills separately from authoritative synthetic venue history. Prefix and
receipt-ID witnesses must not use ClientKnowledge as their expectation.
Supplementary oracle: uninterrupted fold equivalence, explicitly labeled as
same-implementation consistency. Independent review uses separate fixtures and
checks both the contract and implementation. Memory cloning is not crash proof.

## Finite failure matrix

Process death: before temporary creation, after half a temporary write, after
complete write before fsync, after file fsync before replace, after replace
before directory fsync, after directory fsync before acknowledgment, and after
acknowledgment. Exercise a receipt that adds new fills and one that changes
terminal quantity evidence. Parent sends SIGKILL to a waiting child at the
selected hook and a separate child reopens/replays from disk. These are actual
process interruption tests; they do not simulate power loss.

Evidence: newer terminal before older live response; newer partial before older
partial original; late original after query recovery; duplicate envelope;
same fill in a different report; truncated terminal detail; absent snapshot;
unknown-order rejection; incomplete horizon/live residual; mirrored bid/ask.
Full-loop receipts are captured without modifying simulator behavior; independent
small fixtures supply a distinct accounting/identity witness.

Invalid state: truncated/empty checkpoint, malformed/duplicate JSON keys,
checksum error, unsupported version, wrong session/inbox, invalid offset,
offset/state mismatch even with recomputed checksum, identity collision,
noncausal evidence, impossible quantity/side/order binding, incomplete state,
unknown fields, non-finite costs, and limit violations. Inject write/fsync/
replace/directory-fsync errors and require no acknowledgment plus safe reopen.

Preservation: exact pinned source replays of all six prior studies, all previous
documentation unchanged, old defect helpers, full Ruff/format/mypy/coverage,
smoke and canonical audit. Final GitHub CI must check the published head itself.
