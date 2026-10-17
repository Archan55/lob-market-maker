# Simulated retained account authority v3

> Synthetic, uncalibrated protocol experiments. No live safety, profitability,
> process exclusivity, account exposure cap or exactly-once effects guarantee.

The separate opt-in `SimulatedAccountAuthorityV3` checks an account's current
owner generation and applies each command effect in the same transaction.
A delayed old-process new command processed after takeover is rejected. Every
successful takeover also permanently disables new entry, including through a
fresh local journal. A pre-existing order remains executable until separately
cancelled: the six-unit witness fills six without cancellation and two with
recovered cancellation. Fencing neither undoes fills nor flattens inventory.

The authority file itself is the modeled venue. SQLite transaction locking
implements this single authoritative state machine; a filesystem lock around a
client journal cannot provide that semantic boundary at an external venue. The
unchanged v2 local lock still coordinates only one retained directory, and its
snapshot still reports `account_fenced=false` and `account_complete=false`.
The v1 permanent quarantine and every previous API/report remain unchanged.

The [pre-experiment contract](ACCOUNT_FENCE_CONTRACT_V3.md) was committed at
`f3bd8fb` before implementation. Previous PR2 head is
`3d261672ec4dc8d55e2c01f59c283e4f85de4bb2`; PR1 remains
`d4104b70660976c6d59fe8915a75ac5616bdecd3`. The
[independent review](ACCOUNT_FENCE_INDEPENDENT_V3.md) uses separate fixtures,
a process worker and seven-unit scalar expectations at price 113.

## Implemented boundary and assumptions

An exact account string in one retained authority has a compare-and-swap owner
registry. Successful claims advance generation, retain owner/session/strategy
context, and atomically disable entry after the first generation. Reused owner
identities are refused. Commands validate the complete context at processing,
including cancellations and duplicate requests. A current owner may cancel an
order accepted for a prior owner. Stale owners cannot produce command effects.
Client identity tombstones survive terminal state, process death and takeover;
immutable command IDs cannot change their payload. Full event replay rebuilds
state, rather than trusting separately stored order or accounting summaries.

Each operation opens a fresh SQLite connection with `BEGIN IMMEDIATE`, FULL
synchronous rollback-journal commit, bounded checksummed state and strict event
validation. No live exchange command is sent after that transaction: the effect
is contained within it. SIGKILL before commit rolls back; after commit the return
can be lost while retained evidence remains discoverable. This tests process
death on a retained local filesystem, without establishing host power-loss,
distributed failover, adversarial storage or rollback durability.

The `FencedRecoverySessionV3` adapter combines this authority with unchanged
`OutboundRecoverySessionV2` reservations and cancellation-only reopen. Its
`capture` verifies exact retained authority/account report provenance before
conversion and durable local capture. Genuine old-generation fill evidence
remains acceptable. Missing acceptance, fill detail or terminal delivery cannot
be inferred from transport success. Four cancellation attempts can exhaust with
uncertainty retained; no new entry is rearmed. No new request is automatically
retried after a lost return.

Authority bounds are eight accounts, 64 claims and 64 retained orders per
account, 256 distinct command IDs and 1024 events per account, and 4 MiB total
encoded state. Refusal at capacity can prevent progress. The complete modeled
universe starts at zero inventory and receives explicit side/quantity trade
budgets, filling retained FIFO orders at their limits with zero fees/rebates.
It is separate from historical L2 matching; completeness and trusted token
issuance are assumptions, not properties inferred from local journals.

## Measured outcomes and counterexamples

| Experiment | Practical result |
|---|---|
| Old process holds a new command across takeover, both sides | Processing rejects stale context; zero orders/fills. Local-only v2 journals in separate directories can reserve twelve units for the same account. |
| Six units accepted before takeover | Order remains executable: six fills without cancellation, two with recovered cancellation; four residual units cancelled. |
| Two contenders for the same generation | One CAS succeeds; the other refuses. A controlled process holds an uncommitted transaction while another attempts the same authority, preserving the actual effect order. |
| Old new/cancel after ownership change | Both refuse, including replay of a formerly accepted command identity. Current-owner new also refuses because entry is permanently disabled. |
| Genuine terminal receipt from the wrong account | Pinned initial source suppresses own cancellation and permits seven fills; corrected capture rejects it, performs own cancellation, and permits zero later fills. |
| Terminal delivered before old fill detail | Cancellation work ends while detail accounting is incomplete; delayed/reversed/duplicate detail later books once. Independent bid/ask witness executes three units, cancels four, and books inventory ±3 / cash ∓339 ticks. |
| All four cancellation requests lost | Local work exhausts with full uncertainty; six primary or seven independent units can still execute. |
| Cancellation succeeds but all four receipts are lost | Actual primary fills are zero; local work still exhausts unresolved. Retained authority receipts remain separate from local acknowledgment. |
| Actual death before/after ownership, new or cancel commit | Complete old or committed state; committed effect discoverable despite lost return. No partially committed generation/effect is observed. |
| Same current token copied to competing processes | Both are valid: twelve primary / fourteen independent units can execute. The token names an owner context, not an OS process or an exposure budget. |
| Local journal rollback | Current authority rejects old tokens, but lost saved reservations can hide already accepted exposure. A six-unit old order still fills. |
| Authority rollback | Old generation and entry permission revive. A revoked token again submits six primary / seven independent executable units. |
| Same configured authority ID on a second DB; alternate account spelling | Each is a separate universe: twelve units across two DBs or an account alias. There is no federation or canonical external account identity. |

The primary raw-event oracle independently reconstructs successful ownership,
identity uniqueness, signed quantities, cash and every event-prefix conservation
identity: original = executed + remaining + cancelled. It does not import the
production fold, exchange, queue or portfolio oracle. Received client bounds
cover authoritative saved-order inventory at the checked evidence prefixes.
The independent reviewer separately derives quantity and cash from unique raw
fill identities, including both signs, delayed detail and duplicate delivery.

There are 19 primary scenarios and 12 independent witnesses, including ten real
SIGKILL deaths. Their stable evidence is 33 JSON files reproduced byte-for-byte
in two complete runs. Controlled contention includes full deterministic event
chronology. Actual free-running races retain inputs, replies and ordered states
in `independent/runtime-races.jsonl`; their scheduler-dependent chronology is
explicitly excluded from the canonical byte comparison. No schedule-selected
winner is presented as a deterministic raw trace.

## Pinned defect and preservation

Initial implementation `00d22c496b536ad52f8db0f0a87c5f09120d79bc` exposed bare
conversion followed by v2 capture. A genuine foreign terminal sharing the client
ID could terminate own cancellation work when acceptance was lost. The exact
same independent executable uses that initial conversion flow only when the
corrected source-bound method is absent. Before: foreign terminal accepted,
own cancellation suppressed, seven fills and -791 cash ticks. After: foreign
terminal rejected, own cancellation issued, zero fills and zero cash ticks.
The unchanged independent regression also fails before and passes after; source,
test and witness hashes, raw reports, chronological effects and JUnit logs are
retained. Existing v2 capture is intentionally unchanged: direct bare conversion
and v2 capture bypasses the source-bound v3 adapter.

[Verification JSON](ACCOUNT_FENCE_V3.json) hashes the canonical new evidence and
records **326 prior v2 JSON artifacts** equal against exact preceding PR2 source,
and **all 41 prior documentation files** unchanged. Existing verifiers separately
repeat/preserve the earlier active, durable, reordering, reconciliation, transport,
client-stop, exposure, latency and queue studies. Old defect witnesses are kept.

Reproduce from the repository root with installed development dependencies:

```sh
python -m pytest tests/integration/test_account_fence_v3.py tests/integration/test_account_fence_independent.py -q --account-fence-output experiments/account-fence-v3
python tools/verify_account_fence.py
python tools/reproduce_account_fence_defect.py
python -m pytest --cov=lobmm --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m mypy src/lobmm
```

CI checks out the exact PR head, runs the aggregate suite and all old/new
reproduction and preservation helpers, then uploads the new journals, authority
events, receipts, scalar audits, raw race chronology, pinned witnesses and
observed environment. Its terminal result and exact SHA are recorded in the
existing draft PR2's description after verification.

## Remaining boundary

No live venue fencing exists here. A real external venue, different authority,
account alias or caller bypassing this gate is outside the modeled universe.
Trusted current tokens are transferable; there is no single-process authority,
credential security, external account canonicalization, authoritative starting
balance import, independent anti-rollback watermark or complete external order
universe. Missing receipts/detail and retry exhaustion can remain unresolved.
No active strategy, rearm or liquidation path was introduced.

The practical next experiment is a separately specified independent authority
incarnation/watermark and token issuance model: challenge copied current tokens
and authority restore against a retained independent witness before considering
any entry recovery. A new client-side lock would not address those failures.
