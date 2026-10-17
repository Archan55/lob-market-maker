# Deterministic transport faults, version 1

This constructed offline study extends PR2's finite client stop to dropped and
duplicated order, cancel and execution-report messages. It tests execution and
knowledge mechanics, not strategy profitability or real venue reliability.
The reliable default remains unchanged. PR1 remains pinned at
`d4104b70660976c6d59fe8915a75ac5616bdecd3`; the prior PR2 evidence at
`6ae69bd876b938c6d7af454849b4f0caf4160c35` remains separately reproducible.

## Bounded protocol

`run_backtest(..., transport_faults=TransportFaultPlan(...))` opts into the
research model. It requires `shutdown_policy: client_stop`, explicit stop and
observation timestamps, and mark accounting. YAML and CLI defaults stay reliable.

Each immutable rule names `new_orders`, `cancellations` or `execution_reports`,
a zero-based send ordinal, and `drop` or `duplicate`. A drop schedules no receipt.
A duplicate schedules two copies of the same envelope at the same causal time;
the receiver applies that envelope identity once. No duplicate order is created,
cancel transition applied twice, or fill cash booked on report delivery.

One send samples latency once. Even a dropped send advances the ordered channel's
nominal delivery watermark. Duplicates preserve source ordinal, report/fill IDs,
payload, latency, and scheduler parent. These are protocol assumptions, not an
inference about a brokerage. Unused rules are disclosed. There is no market-data
fault mode, generic retry, timeout, reconnect, recovery query or persistent inbox.
The existing stop controller can retry an early unknown cancel after delivered
acceptance; a lost cancel without that trigger can remain unresolved indefinitely.

The inclusive observation horizon processes every same-time causal wave.
Historical activity precedes same-time cancellation. Commands and expiry after
tape coverage remain censored; frozen depth cannot establish intervening fills.
The fault plan is available to evaluation and transport only, never to strategy
signals or client stop decisions. No networking or external service is involved.

## Venue truth and client evidence

| Quantity | Evidence and meaning |
|---|---|
| Covered venue cash/inventory | Actual unique exchange fills through tape coverage; cash booked at fill time |
| Venue reachable inventory | Covered inventory plus every possible partial fill of live or travelling entries; dropped entries cannot reach the venue |
| Known inventory | Only unique fill reports delivered by the horizon |
| Client inventory bounds | Sent quantities and delivered remaining-quantity snapshots; no access to venue state or fault selection |
| Client reservations | Unresolved sent quantity minus notified fills; retained across silence and unknown-order cancellation rejection |
| Missing fill quantity | Execution established by a delivered snapshot but absent from notified fill detail |
| `accounting_complete` | No unresolved venue order outcomes in the covered model; retained inventory can be nonzero |
| `client_knowledge_complete` | All sent entries have terminal evidence and all executed units have delivered fill detail |

A terminal acknowledgment resolves possible future fills of that order. It does
not manufacture a fill notification or execution price. If earlier fill reports
were lost, the client can infer total executed quantity from original minus
terminal remaining quantity while still lacking fill details and accounting
reconciliation. That inferred inventory is separate from the strategy's known
inventory. Actual dropped entries are disclosed separately from travelling
entries, while the unaware client keeps their reservations.

Fault runs save `transport_faults.json` containing the versioned plan, every send,
nominal time, selected action, each receipt and duplicate suppression, plus unused
rules. Summary and diagnostics disclose the plan; human-readable reports show
client uncertainty. Ordinary comparisons reject different fault plans. A plan
belongs to the saved input identity even though ordinary YAML is unchanged.

## Independent study and complementary checks

The [JSON audit](TRANSPORT_STRESS_V1.json) covers **170 full replays**: systematic
single drops/duplicates, explicit paired post-cancel witnesses and **32 seeded
adversarial combinations**. All three strategies and both directions are tested;
signal coefficients are neutralized to isolate execution mechanics. Entry,
cancel and report delays include zero, delayed entry after stop, horizons before
or at receipt, and commands after tape end. Trades exercise partial and full fills.

The audit checks **5,995 checkpoints** and **34,945 enumerated client inventory
possibilities**, with 267 unique fill records and 88 suppressed duplicate
deliveries. At the selected horizons, 86 cases retain incomplete client knowledge
and 42 retain unresolved venue exposure. Fourteen executed units lack delivered
fill detail despite later quantity evidence. These are disclosed modeled
outcomes, not test failures.

Observers copy state after original handlers. Expected values come from a
separate scalar FIFO budget, integer tick cash, Decimal cost arithmetic,
independent aggregate-depth marks and an expected report/envelope delivery queue.
Every generated report, absence of delivery, duplicate receipt, causal wave,
quantity/state trajectory, cash/inventory snapshot, public fill row and final
diagnostic is checked. Client bounds and venue reachability exhaust partial-fill
combinations independently of engine queue, risk, portfolio or transport helpers.

Nine corruption controls include altered cash, knowledge, quantities, report
generation and causal time, an internally consistent missing acceptance, and a
duplicate with a new identity. They establish that checking only final cash or
internally consistent client state would be insufficient.

**58 complementary public-output tests** use separate closed-form expectations
and ordinary unobserved backtests. They cover one-nanosecond cancel boundaries,
zero latency, all-duplicate economics, missing partial-fill reports, no timeout
release across very long horizons, explicit venue expiry and partial taker fills
after stop with an acceptance-triggered cancellation retry. Six pairs compare
all eight replay tables and YAML byte for byte against the reliable default.
Two additional client-boundary tests check report/fill identity suppression.

No new reliable-engine defect was established or fixed by this extension.
Duplicate packet handling and incomplete-report reconciliation are explicitly
new protocol semantics. Earlier genuine defects remain demonstrated by the
existing pinned-source helpers before their fixes: 22 exposure/mark failures with
four controls, and three unknown-cancel knowledge failures.

## Findings

| Constructed witness | Measured result |
|---|---|
| Trade at cancellation versus one ns later | Historical-first tie can fill; the later trade cannot fill after a delivered cancel |
| Same later trade with the cancel dropped | Two additional own units fill; remaining venue exposure stays unresolved |
| Partial fill report dropped, terminal cancel acknowledgment delivered | Venue inventory is two units, notified inventory is one; terminal evidence identifies the missing unit and completeness remains false |
| Both entries dropped | Venue has no order/fill and accounting is complete; client retains eight reserved units and inventory bounds `[-4, 4]` |
| Every order/cancel/report duplicated | Replay economics and fill notification times equal reliable transport byte for byte |
| Partial taker arrival after stop | Two units book at arrival, two rest; lost fill detail does not change actual cash or invent known inventory |

Counts of incomplete outcomes describe this selected case set, not probabilities
of transport failure or estimates of real risk. Marks remain historical research
marks and residual inventory is unliquidated.

## Reproduce and preserve evidence

```bash
python -m pytest tests/integration/test_transport_stress_v1.py -q \
  --transport-study-output experiments/transport-stress-v1
python -m pytest tests/integration/test_transport_complementary.py -q
python tools/verify_transport_evidence.py
python tools/reproduce_exposure_defects.py
python tools/verify_client_stop_baseline.py
python -m ruff check .
python -m ruff format --check .
python -m mypy src/lobmm
python -m pytest --cov=lobmm --cov-report=term-missing \
  --transport-study-output experiments/transport-stress-v1
```

Replaying a saved case requires its saved plan as well as its tape and YAML:

```python
import json
from pathlib import Path
from lobmm.backtest import run_backtest
from lobmm.config import load_config
from lobmm.data.loaders import load_events
from lobmm.transport import TransportFault, TransportFaultPlan

case = Path("experiments/transport-stress-v1/execution_reports-2-drop__fixed_spread__1")
raw = json.loads((case / "transport_faults.json").read_text())
plan = TransportFaultPlan(tuple(TransportFault(**row) for row in raw["faults"]))
run_backtest(
    load_config(case / "run_config.yaml"),
    load_events(case / "tape.csv"),
    run_name="repeated",
    transport_faults=plan,
)
```

The verifier extracts pinned prior PR2 source using local `git archive` and runs
all three previous studies in one installed environment. **1,958 deterministic
artifacts are byte-identical**: 600 latency, 378 exposure and 980 client-stop.
No correction to economics or earlier published evidence is needed. It also
repeats the new study and checks every hashed file and the audit JSON byte for
byte. Performance wall time is excluded; cross-version Parquet bytes are not
claimed. CI checks out the PR head explicitly and uploads tapes, plans, tables,
snapshots, normalized outputs, baseline comparisons and observed dependencies.

## Limits and next step

This model covers deterministic loss and same-time duplication with stable
message identities. It does not cover reordering, altered/corrupted payloads,
duplicates assigned new logical IDs, retransmission semantics, process restart,
durable deduplication, venue cancel-on-disconnect or reconciliation APIs. Most
cases use passive FIFO matching; separate public tests cover partial takers.
The study does not establish all queue policies, calibrated jitter, market
impact, real transport reliability or profitability.

The next bounded research question is an explicit, finite reconciliation policy
that can resolve lost fill details and terminal acknowledgments using a modeled
venue query. It must preserve the present unknown outcomes as the no-recovery
baseline and remain offline.
