# Independent authority witness v4 — contract before implementation

Baseline: PR2 `91675f77738dda6940072cdaba565c455ae2f274`, stacked on unchanged
PR1 `d4104b70660976c6d59fe8915a75ac5616bdecd3`. The saved environment has no
AGENTS.md, tasks directory or .agents skills. Prior APIs, source, reports and
counterexamples stay intact. This is opt-in synthetic, uncalibrated research.

## Evidence and bounded protocol

A new cancellation-only facade wraps the existing v3 synthetic venue. Its
separate SQLite witness is assumed retained independently of venue/client
restore operations. This is an explicit conditional fault assumption, not a
claim that another local file supplies independent durable infrastructure.
Initial enrollment trusts the exact existing authority bytes as a baseline;
it cannot detect exposure or rollback predating enrollment. An explicit
incarnation name and authority ID bind one enrolled lineage to one witness.
No automatic registration/open on missing evidence, namespace reset or rearm.

The witness retains a full-body authority SHA-256, a monotonic operation watermark,
a pending write intent and bounded immutable issuance records. A serialized
witness transaction validates the authority digest and reserves an exact proposed
digest, then commits that intent BEFORE the venue commit. A second witness
transaction stays locked while the venue commits and the witness finalizes.
A contender during the prepare/lock gap sees pending intent and refuses work.
There is deliberately no distributed atomic-commit claim. Any pending intent
surviving process death freezes all subsequent facade operations, even if the
venue is at the old or proposed digest. It cannot be safely aborted: an effect
may have committed and then been rolled back. Witness ahead, lag, missing,
unavailable or malformed evidence all fail closed. No automatic repair.

Each issuance has a caller-supplied immutable request ID and the exact
owner/session/strategy/expected-generation payload. Lost successful responses
can be looked up/retried with that ID without another generation increment.
Conflicting reuse refuses; distinct same-generation contenders have at most one
successful CAS. Returned tokens bind witness ID, incarnation, issuance ID and
the full v3 ownership token. Historical issuance lookup does not reactivate a
stale token. Copied current tokens remain transferable and indistinguishable;
this subset does not promise process exclusivity. Every new-order dispatch is
refused, including copied tokens. Current issued owners may cancel old accepted
orders. Market trade budgets remain possible while client cancellation is blocked.

Bounds: one authority lineage and witness per facade; 64 issuance records,
4096 mutations, 4 MiB witness bytes, plus unchanged v3 account/order/event bounds.
No credential/authentication, hostile tamper, hash collision, power-loss,
account alias federation or complete external venue inventory guarantee.
The witness and authority use separate fresh SQLite connections and FULL
synchronous transactions on local POSIX files. Direct v3 calls bypass this facade
and are explicitly outside its boundary. Enrollment must occur while the modeled
venue is quiescent. Arbitrary callers able to mutate either file are not fenced.

## Acceptance and failure criteria fixed before experiments

Before mutation and read, require exact retained authority/witness digest match,
no pending intent, matching identity and bounded valid witness state. Reserve
intent before authority commit; finalize only against the reserved old/proposed
hash. A rejected operation leaves venue effects and witness watermark unchanged.
Issuance retries recover the same token; cancellation retries retain v3 immutable
command semantics. All facade entry stays quarantined. Preserve accepted versus
unaccepted commands: accepted old orders can still fill; unaccepted new commands
never enter via this facade. Cancellation does not undo executed inventory.

Use actual interpreter processes for competing issuance and SIGKILL before intent,
after intent, after venue commit and after witness finalize. Retain operation
inputs/replies, witness states, raw venue events and scalar quantity/cash outcomes.
Exercise venue-only, witness-only, client-only and combined rollback, lost takeover
responses, witness unavailability/lag, duplicate issuance, copied current tokens,
namespace reset, stale accepted/unaccepted commands and cancellation exposure.
A separate reviewer must challenge this protocol and derive identity/quantities/
signed exposure directly from raw inputs/events, without the production fold or
primary test oracle. Pin any actual implementation defect before fixing it, with
an unchanged reproducer failing before and passing after. Negative model
counterexamples are findings, not bugs to hide.

Restoring BOTH authority and witness to a matching old prefix is undetectable
without a third independently retained observation. Demonstrate equal visible
states with different external executed histories. Neither locks nor checksums
supply that missing information. Copied current tokens cannot prove a sole
writer's process identity. Frozen cancellation can leave executable old orders;
report its quantity/cash cost rather than calling fail-closed safe trading.

Repeat canonical evidence byte-for-byte, preserve all prior tracked source/docs
and v3 evidence against the exact baseline, run the full aggregate and previous
verification helpers, push existing draft PR2 and wait for exact-head CI terminal
result. No entry recovery, live readiness, exactly-once or profitability claim.
