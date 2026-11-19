# Retained maker-fill trigger provenance, 2026-11-19

Preregistered before any new fill-to-market joins, classifications or numerical
outcomes. These synthetic records are exposed; this is a retrospective model
diagnostic, not a fresh holdout or real execution calibration.

## Question, rationale and decisions

What fraction of the fixed ordinary-workload emitted maker fills is associated
with the explicit price-through rule, rather than an exact-price trade? Can
every emitted fill be uniquely attributed and satisfy the corresponding order
quantity and printed-trade budget constraints?

The primary hypothesis is complete, unambiguous attribution with zero violated
invariants in both selected cohorts. Secondary results are exhaustive descriptive
counts and exact quantity/notional shares, including zero shares. No favorable
share threshold, profitability hypothesis or significance test is defined. A
large through share indicates reliance on that modeling assumption; zero share
means these cohorts do not exercise it. Neither result establishes real fills.

PR15 checked accounting conditional on supplied fills and marks. PR1's controlled
cancellation witness disabled price-through. Neither establishes reliance on this
rule in the normal PR14 records. This question adds execution-model provenance
without redoing accounting, rerunning an engine or sweeping model parameters.
Exact-price fills also depend on estimated queues and an exogenous tape; they
must never be labeled calibrated, reliable or observed venue executions.

## Immutable sources and exposure history

Base: PR15 `95dbd73d75ffc95b127d9c21e825e940ce230d0f`. Production remains the
38-file C1 composition at PR13 `0866bf13e449cd66e3862454eaaf424a5172c33a`.
Original data baseline: PR14 `ec7b6e0bce491d508c30a44b737950cda503d597`.
Use only the original selected directories below:

| Cohort | Events | Directory under docs/evidence/end-to-end-resources-20261117/run/attempts/ |
|---|---:|---|
| 001 | 2,000 | 001 |
| 002 | 20,000 | 002 |

Pin input/events.parquet, engine/resource-study/fills.parquet, orders.parquet,
run_config.yaml and result.json, the original PR14 manifest/plan and event-stream
fingerprints. Seal analyzer, mathematical controls and this design in Git and
an immutable input/source plan before the first pass. Keep all earlier heads,
original records and PR15's stale progress-field disclosure unchanged.

Both cohorts are seed-7 constructed inputs previously inspected by PR14/15.
Their generator/prefix overlap; they are not two independent market sessions.
Prior accounting outcomes and counts (26/780 maker fills) are known. Trigger
classifications and new joins have not been inspected. Runs 003/004 are duplicate
resource repetitions and are excluded. No new input, seed, window or source may
replace these cohorts after outcomes.

## Admission and identifiable quantities

Read the pinned configuration as literal text. Require the fixed price-through
rule enabled, post-only strategy and MARK session-end policy, with unchanged
fee/tick settings. Costs are context for the saved model, not evidence of
calibrated costs or a profit comparison. Currency notional, if displayed, is exact
quantity times fill-price ticks times the literal tick value.

Independently reconstruct the complete original external tape using integer
depth dictionaries: RESET clears; ADD/SNAPSHOT add; CANCEL/TRADE reduce only
available external quantity. Require exact six-column types, nondecreasing
timestamps, strictly increasing global sequence, positive nonreset price/size,
valid resting side and an uncrossed book after each event. No lenient clamping
or imputation. Recompute the original event fingerprint. This justifies using
raw trade quantity as its applied external quantity in these fixed records.
Record the original exchange validation mode unchanged: independent strict tape
reconstruction does not imply the original exchange was configured STRICT.
Post-only maker activity and MARK termination do not consume its external book.

Require unique nonempty fill IDs and transition IDs, a nonempty order ID on every
registry transition and fill, and one consistent immutable identity per order ID
across its repeated transition rows. Preserve original retained row order. The
pinned producer emits registry rows only; do not silently skip blank-ID rows.
All selected fills must be makers with positive integer price/quantity and a
valid side. Every fill timestamp must have exactly ONE canonical input EVENT,
which must be a matching-side TRADE. Missing, multiple, other-type, wrong-side
or worse-price associations stop admission; never guess a last same-time state.

Join fills to their immutable order ID, client/strategy IDs, side, limit price,
original quantity and decision/send/arrival times. These fields must agree
across that order's transition rows. Every maker execution must be at its own
limit. Require decision <= send <= arrival < associated trade timestamp <=
notification under this engine's market-before-command phase convention.

Reconstruct remaining quantity from original quantity minus prior fills in
retained per-order fill order; it never becomes negative. Match every fill
one-for-one to that order's immutable reason=fill transition by original order,
timestamp, resulting partial/filled status and remaining quantity. Validate the
transition previous/new status chain and remaining checkpoints. A same-timestamp
cancellation may follow a partial fill: respect stored transition order rather
than rejecting equality. Non-fill transitions do not consume quantity.

The appended cumulative-filled/average/queue-ahead/behind columns repeat final
state. Never use them as historical queue priority or a fill-time trajectory.
This is a consistency check on emitted fills, not a complete command/report
archive, pending-cancel reconstruction, FIFO proof or missing-fill detector.

## Classification, budgets and exhaustive outputs

For each admitted maker fill:

- Exact-price: fill limit equals the unique trigger trade price.
- Price-through: BID limit strictly exceeds that trade's BID price, or ASK limit
  is strictly below that trade's ASK price.

No third guessed class is allowed. Through quantity must equal the order's
independently reconstructed pre-fill remaining quantity. Exact quantity cannot
exceed remaining quantity. Sum all exact-price own fill quantity associated with
one trade and require it <= that event's raw/applied printed quantity.

Do NOT charge through fills against that same printed budget. The documented
model first sweeps eligible better own levels, then spends the exact-price
budget. Through-plus-exact quantity greater than printed quantity is therefore
an assumption diagnostic, not an overspending defect. Do not infer market
response, hidden liquidity or a counterfactual run with this rule disabled.

Per cohort report both classes' fill/order/trigger-event counts, exact executed
units and fill-price tick notional, their exhaustive exact-rational shares, and
total coverage. Order/event sets may overlap between classes and must be labeled
accordingly. Report through distance in ticks (minimum/maximum and unit-weighted
mean), and counts/ratios of through units versus printed quantity per associated
event. Preserve zero counts and null denominators without selected subsets.
No P&L deletion, residual profit estimate, fee tuning, toggle run or fill-quality
ranking follows from these shares.

## Budget, preflight controls and stopping

One primary offline pass, 001 then 002, with no engine/resource trial, application
imports or network acquisition. Caps per cohort: 100,000 input rows, 100,000 order
transitions, 20,000 fills and 120 seconds. Total controller lifetime 300 seconds;
generated evidence 64 MiB. These are modest experiment caps, not capacity targets.
Refuse existing output paths. Preserve per-fill classifications, per-event budget
records, every encountered failure and complete or stopped-prefix status, frozen
source/plan and exact runtime/source hashes. No automatic retry.
After a stop, unprocessed suffixes are explicitly untested, not silently accepted.

Freeze fixed hand controls before the first pass: both-side exact/through/worse
relations; ambiguous timestamp rejection; exact shared-budget violation; through
quantity above printed quantity permitted; partial close followed by full
remaining through fill; same-time post-fill cancellation; wrong order/transition
remaining; and ignored retrospective queue/cumulative metadata. These controls
test the analyzer, not additional market observations.

Stop after the fixed pass. Failed admission or invariant is a negative/stopped
finding, with no changed threshold, cohort, class or dropped record. There is no
post-outcome analyzer repair/reanalysis budget in this study. Independent review
may diagnose a failure from retained evidence; any subsequent implementation or
new experiment needs a separately justified design. Independent design/source
review precedes outcomes; independently authored result/artifact review follows.
Explicit CI arithmetic repeats, if published, use these same exposed records and
are verification rather than new trials. Standard inherited correctness tests
remain separate software checks.

No optimization, stopped November 2/4 candidate, PR12 gate change, extra recovery
scaffolding, paid service/data, private/licensed E1, unapproved E2, denied-route
retry, access/security change, desktop, merge, deployment or trading is involved.
Actual strategy evaluation still needs admitted tapes and execution calibration.
