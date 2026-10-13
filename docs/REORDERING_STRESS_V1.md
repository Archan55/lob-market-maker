# Out-of-order recovery evidence, version 1

This PR2 extension adds deterministic delay/reordering for selected execution
reports and reconciliation responses. It preserves finite query semantics and
all prior versioned studies. The exact prior PR2 source is pinned at
`3da58b38b49108cc116fa4910d77da689a1ab922`; PR1 remains unchanged at
`d4104b70660976c6d59fe8915a75ac5616bdecd3`.

## Minimal delivery extension

`TransportFaultPlan(..., delays=(TransportDelay(...), ...))` accepts at most 16
immutable extra-delay rules. Each selects a supported evidence channel and its
zero-based send ordinal. The extra delay is a positive integer in
`1..10**12` nanoseconds (a finite research bound, not empirical calibration).
Rules are unique per channel/ordinal. Commands, market data and query requests
cannot use this delay extension.

The channel first computes its existing nominal delivery time, including its
ordered latency clamp. It then adds the selected extra delay for that envelope.
Subsequent messages retain their own nominal schedule, allowing them to overtake
the delayed message. Equal-time receipts keep scheduler causal-wave, phase and
source-order rules. The delay changes no matching, economic execution time,
response snapshot, payload, fill/report ID or envelope identity.

Extra delays compose with existing drops and duplicates. Duplicate copies retain
one identity, share the effective arrival time and are suppressed on receipt.
Drops still have no delivery. Unused delay rules are disclosed. For selected
plans, sends save nominal time, extra delay and effective time; actual receipts
and pending channels remain separately visible at the inclusive finite horizon.
An empty delay tuple preserves the exact v1 plan and artifact shape. Plans with
delays use `transport-faults-v2`. Run comparisons reject different delay plans.

```python
from lobmm.transport import TransportDelay, TransportFaultPlan

faults = TransportFaultPlan(
    delays=(
        TransportDelay("reconciliation_responses", 0, 80),
        TransportDelay("execution_reports", 2, 120),
    )
)
# Pass faults as transport_faults to run_backtest with finite client_stop.
# Query plans remain separate, finite ReconciliationPlan instances.
```

## Defect demonstrated before correction

The original strategy cache assigned a partial report's remaining quantity even
when a newer partial report had already arrived. In a six-unit own order, the
first execution fills two units at 40 and the second fills two at 70. Delaying the
first report until 140, while its terminal report remains pending past horizon
200, produces this sequence:

| Receipt | Original cached remaining | Corrected cached remaining |
|---|---:|---:|
| Newer partial at 70, cumulative execution four | 2 | 2 |
| Older partial at 140, cumulative execution two | 4 | 2 |

The client inventory already had shared fill-ID deduplication and its evidence
bounds were monotone. The defect was confined to the strategy's cached remaining
quantity. The correction takes the minimum of its existing value and the
reported value. Late terminal-before-acceptance handling already retired stop
controller work without resurrection; no further change was required there.

`tools/reproduce_reordering_defect.py` loads the exact pinned original strategy
in a separate subprocess while retaining the newly added delay transport. All
three strategies and both directions fail the monotone cache assertion before
correction (**six failures**) and pass afterward (**six passes**). Logs and the
pinned source are retained in `experiments/reordering-defect-v1`. This demonstrates
a previously unsupported delivery-order defect; it does not claim old ordered
or reliable studies were incorrect.

## Independent full replay and complementary review

The [JSON audit](REORDERING_STRESS_V1.json) covers **72 paired cases / 144 full
replays** and **2,688 client checkpoints**. All three strategies and both sides
use neutralized signals. Twelve modes exercise newer terminal responses before
older live responses, truncated snapshots after newer fill reports, absent older
snapshots after complete recovery, recovered detail before late originals,
reversed partial reports, late original acceptance, duplicated reordered
responses, finite horizon boundaries, live outcomes beyond tape, full-fill races
and same-time response ordering.

Expected history comes from a scalar witness: four external units ahead of six
own units; trade six consumes four external plus two own, and a trade at the
cancel boundary consumes another two own (or four for the full-fill witness).
Historical trades precede cancellation at equal time. The expected delivery
partial order is derived from those immutable sends and selected delays. The
oracle does not call the production exchange, queue model, journal, client
knowledge or portfolio to obtain expected results.

For each order the oracle intersects quantity intervals from delivered evidence
and unions unique execution identities. It checks each checkpoint's notified
inventory, quantity bounds, terminal evidence, missing detail, reservations and
managed partial-report quantity. Independent integer cash and Decimal costs are
checked against the client ledger; public fills, earliest notification and every
saved cash/inventory/P&L snapshot are checked against the scalar execution history.
Query snapshots are independently reconstructed at their frozen cutoff. A later
receipt never acquires later venue events retroactively.

Each recovery run is paired with the same transport plan without reconciliation.
All eight venue/configuration artifacts are byte-identical; fill economic fields
match exactly while notification times may reflect earlier recovered detail.
Five corruption controls reject wrong inventory, intervals, reservations, cached
remaining quantity and delivery time. A separate comparison control rejects
changed delay plans.

A separate **36-case public-output review** uses a different scalar witness: three
external units ahead of five own, executing two then three. It imports no main
study fixture, observer or oracle. It verifies old-live/new-terminal response
inversion, duplicate suppression, a truncated terminal response that remains
incomplete until its late original detail arrives, earliest notifications,
covered venue cash, and horizon 119 versus inclusive arrival 120. Fourteen unit
checks cover delay bounds, rule limits, drop/duplicate composition and unused
rules.

## Tested findings

| Constructed outcome | Evidence |
|---|---|
| Newer terminal response at 100, older live response at 160 | Terminal quantity bounds and zero reservations remain; stale detail contributes no duplicate fill |
| Absent old response delivered after complete newer response | Its empty result erases no identities, quantities or terminal evidence |
| Query recovers fill detail before delayed originals | One client booking per fill ID; original arrival cannot replace earlier notification |
| Truncated terminal snapshot | Executed quantity can be exact while missing price/cost detail keeps client completeness false |
| Terminal report pending beyond horizon 200, no query state/detail | Four units are known; the target's remaining two units stay reserved and its outcome remains uncertain |
| Dropped cancellation, response generated after tape end 250 | Covered four units are learned; live residual exposure still ranges from four to six executed units |
| Old response pending beyond horizon 159 | Pending transport remains visible even when independent newer terminal evidence already resolves the order |
| Equal-time responses | Source order within each causal wave remains deterministic |

There are **48 incomplete client outcomes without recovery and 12 with recovery**
among the selected pairs. Queries recover **204 execution units** through **138
received responses**. Six client outcomes remain uncertain because terminal
reports are beyond the finite horizon; six retain unresolved live venue exposure
beyond tape coverage. **Six venue outcomes remain incomplete** with and without
recovery. These are constructed-case counts, not probabilities or profitability
estimates. Residual inventory is retained and marks have historical provenance.

## Volatile checkpoint test and deferred restart

Four clearly separated client-only tests clone live Python client/runtime objects
with `deepcopy` after either a newer original report or a complete recovery
receipt. Both the uninterrupted objects and volatile clones replay a suffix of
recovery detail, late original reports, a new report ID for an existing fill, and
an older live snapshot. Knowledge, execution detail and notified inventory agree;
terminal reservations do not reopen. Stable fill/report identities must survive
in that retained in-memory state for this result to hold.

This is a volatile checkpoint replay test. There is no serialization format,
durable checkpoint store, inbox offset, atomic ledger/checkpoint transaction,
process death, scheduler reconstruction or persistence guarantee. It adds no
production restart API. Durable restart remains deferred.

## Validation and preservation

Cloud Python 3.12.14: **1,287 tests passed / 92.14% branch-aware coverage**.
Ruff lint/format and strict mypy passed.
The 300-event smoke and canonical nine-case queue audit pass. Both earlier pinned
before/after defect helpers remain reproducible.

Offline extraction of pinned prior PR2 source confirms **all 9,548 prior stable
artifacts byte-identical** in one installed environment: 600 latency, 378 exposure,
980 client-stop, 2,550 transport and 5,040 reconciliation artifacts. Every prior
study audit JSON also matches; no prior study or economic correction was needed.
The new complete repeat matches **2,160 artifact pairs** and byte-identical audit
JSON. The published new JSON matches the repeated generated evidence exactly.
Cross-version Parquet bytes and wall-clock measurements are not claimed.

```bash
python -m pytest tests/integration/test_reordering_stress_v1.py -q \
  --reordering-study-output experiments/reordering-stress-v1
python -m pytest tests/integration/test_reordering_complementary.py \
  tests/unit/test_transport_delays.py -q
python tools/reproduce_reordering_defect.py
python tools/verify_reordering_evidence.py
python tools/verify_reconciliation_evidence.py
python tools/verify_transport_evidence.py
python tools/reproduce_exposure_defects.py
python tools/verify_client_stop_baseline.py
python -m ruff check .
python -m ruff format --check .
python -m mypy src/lobmm
python -m pytest --cov=lobmm --cov-report=term-missing \
  --reordering-study-output experiments/reordering-stress-v1
```

Saved replay plans load both `faults` and optional `delays` from
`transport_faults.json`, constructing `TransportFault(**rule)` and
`TransportDelay(**rule)` respectively. Load the unchanged finite query fields
from `reconciliation.json`, and the saved `tape.csv` and `run_config.yaml`.

CI checks out the exact PR head, repeats the new evidence, verifies all pinned
prior studies, reproduces the defect and uploads the complete inputs, plans,
frozen responses, traces, tables, repeats, comparisons and observed dependencies.
PR2 stays a draft dependent on unchanged PR1. All existing versioned reports are
preserved. No networking capability, brokerage, deployment or trading is added.

## Limits

The journal still assumes retained immutable simulated executions and stable,
never-reused order IDs. Selected delays move entire envelopes; there is no payload
alteration, independent duplicate-copy jitter, automatic retry, timeout policy,
retention window, pagination, calibrated venue API or active-strategy recovery.
The strategy remains stopped during reconciliation. Delaying a pre-stop report
can affect ordinary strategy decisions; equality of venue outputs is established
for these constructed cases, not for arbitrary active strategies or tapes.
Finite receipt limits do not imply loss or termination, and tape end does not
supply missing future venue outcomes. This evidence proves the selected mechanics
and detected cache correction, not real-market reliability or profitability.
