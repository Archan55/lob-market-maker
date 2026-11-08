# Fixed-horizon markout aggregation — November 8, 2026

This study evaluates a measurement convention in completed simulation audits.
It does not evaluate a market-making strategy or establish economic advantage.
The initial protocol was committed as
`34fdabca867a29c48dfb1664060e01d03957a4dd`, before numerical execution or
implementation. Source baseline is main
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`.

## Question and units

`compute_markouts` reports favorable side-adjusted movement from fill price to
the first true midpoint at or after each requested horizon. `markout_ticks` is
per unit in ticks; `markout_value` is per unit in model currency. The existing
horizon means are event-row statistics, and the side means pool available rows
across horizons. Those are valid descriptive means when labeled accordingly.

The former `adverse_selection_markout` instead multiplied the mean available
per-unit currency outcome across all fill/horizon rows by all filled quantity.
It has no specified single-horizon population and includes quantities whose
outcomes are unavailable. The frozen question is whether a quantity-weighted,
fixed-horizon observed statistic preserves identical unit exposure when fills
are split, while coverage makes censoring visible.

For horizon `h`, and separately for all fills, bids and asks:

```text
Q_all = sum of authoritative fill quantities in the group
Q_observed = sum of quantities with an unambiguous finite outcome at h
A_observed = sum(quantity_i * markout_value_i,h)
weighted_mean_value = A_observed / Q_observed
weighted_mean_ticks = sum(quantity_i * markout_ticks_i,h) / Q_observed
quantity_coverage = Q_observed / Q_all
```

An observation requires both finite outcome fields. No observation gives null
means and amount, with zero quantity coverage when the denominator is positive.
An empty side group has undefined coverage. Observed zero remains numeric zero.
Unobserved quantity is explicit and is never assigned an inferred price or
multiplied by the observed mean. Horizon totals must not be added together.
Different horizon coverage also prevents interpreting them as a common-sample
decay curve.

Quantities join uniquely from authoritative fills. Consistent duplicate horizon
requests count once only in the new summaries. Conflicting outcomes or identity
metadata cannot be resolved by choosing an arbitrary row. Raw markouts and the
existing first-at-or-after timing convention remain unchanged. Empty markout
rows do not identify the configured horizons, so the new table is empty.

## Preregistered controls and selection

The [contract](evidence/markout-aggregation-20261108/contract.json),
[fixed fictional audit rows](evidence/markout-aggregation-20261108/fixtures.json),
[design review](evidence/markout-aggregation-20261108/design-review.json) and
[pre-execution compatibility clarification](evidence/markout-aggregation-20261108/preexecution-clarifications.json)
define the complete study. The static source lead preceded all new outcomes.
The clarification records metadata names, preservation of the raw API's float
price representation, and duplicate/missing-field comparisons before test freeze.

The fixed controls cover unequal quantities, identical-fill splitting, side
symmetry, mixed sides, horizon composition, duplicate horizon requests, late
censoring, different per-horizon coverage, missing rows, finite eligibility,
valid zero, all missing, empty input and unambiguous joins. They are pure
post-run audit-row examples, not a claim that the fictional fills were produced
by a realistic market. The ordinary CI300 wiring pair separately checks a full
backtest with unchanged input and configuration.

There is one aggregation candidate, at most one independently reviewed
correctness repair, and no parameter or timing search. Two already exposed Nov5
cohorts are reanalyzed read-only; no new 20k engine run is permitted. Their effect
sizes and signs are not publication gates. The stopped November 2 and November 4
performance candidates remain closed.

## Output compatibility

The proposed `execution_quality.markout_summary_by_horizon_ns` contains separate
`all`, `bid` and `ask` records. Each reports authoritative total quantity/events,
observed and missing quantity/events, quantity coverage, quantity-weighted tick
and per-unit currency means, and observed aggregate currency value.

The old event means retain their exact definitions and values. The old scalar
becomes explicitly unavailable under `attribution.adverse_selection_markout`;
its exact former calculation is retained as `legacy_pooled_markout_proxy` for
comparison and migration. Adjacent notes explain both conventions. Existing
ledger attribution components and residual algebra remain unchanged. Consumers
must handle the old key's new nullability and choose an explicit horizon rather
than silently substitute a different scalar.

Observed gross forward markouts include entry spread. They do not isolate
causal adverse drift, include costs, or equal terminal/realized PnL. This change
does not add fill-time causal midpoint fields or resolve the end-of-timestamp
attribution convention studied on November 7.

## Results and reproduction

Results, source/test pins, original execution counts, independent artifact review
and reproduction commands will be recorded here after the frozen experiment.
Publication is conditional on the predefined numerical, execution-equivalence
and ordinary quality gates; this working report is not a completed result.

## Remaining economic prerequisites

[Metadata-only feasibility review](evidence/markout-aggregation-20261108/data-feasibility.json)
found no new usable real L2/trade data or execution calibration in the matching
saved LOB executor. Existing inputs remain synthetic or explicitly fictional.
Actual strategy evaluation still needs legally usable multi-session tapes with
reconstruction, gap/sequence and clock provenance; dated instrument and cost
schedules; and defensible queue, priority and execution-latency calibration.
No data was acquired or imported from another executor. Accounting overhead and
output growth remain unresolved.
