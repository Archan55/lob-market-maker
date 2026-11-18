# Offline accounting reconciliation, 2026-11-18

Preregistered before any new independent ledger replay or reconciliation result.
The retained artifacts were previously decoded for other studies and are exposed;
this is retrospective correctness validation, not a fresh financial holdout.

## Question and novelty

Given retained fills, literal economic configuration and recorded midpoint marks,
does an independently authored rational ledger reproduce every persisted portfolio
snapshot projection in the first 2k and 20k PR14 runs? This is an absolute
mathematical comparison, unlike prior same-implementation output equality.
Earlier PR3 hand tapes, PR13 fee/risk cases, stopped November 2/4 timing candidates
and PR14 resource measurements are not rerun or retuned.

The primary hypothesis is zero accounting/projection mismatches across both fixed
cohorts. Any failure remains a negative result; no alternative cohort, changed
tolerance, selected prefix or favorable subset. A pass validates the recorded
accounting conditional on emitted fills/marks, not fill realism, causal mark
selection, hidden exact state, risk handoffs, execution calibration or economics.

## Fixed source and data

Base is PR14 `ec7b6e0bce491d508c30a44b737950cda503d597`. Production source remains
PR13 `0866bf13e449cd66e3862454eaaf424a5172c33a`, all 38 C1 files unchanged.
Use only these retained original synthetic cohorts under
docs/evidence/end-to-end-resources-20261117/run/attempts/:

| Cohort | Events | Selected run |
|---|---:|---|
| small | 2,000 | 001 |
| normal | 20,000 | 002 |

Inputs are fills.parquet, pnl.parquet, inventory.parquet and run_config.yaml in
engine/resource-study/ for each selected run. Pin their original manifest hashes
and the input Parquet/event-stream fingerprints, resource plan and application
source manifest before execution. Runs 003/004 are already byte-identical repeats;
do not treat them as independent samples or rerun them. The two cohorts share an
exposed generator/prefix, not independent market sessions.

No application imports, Portfolio/cost/renderer helpers, engine invocation,
synthetic generation, resource trial, network acquisition or plotting. PyArrow,
YAML and independent standard-library arithmetic may read the fixed artifacts.
The comparator is mathematics, not main or an optimized candidate. Any new
branch/draft is stacked on PR14; all earlier heads and draft states stay intact.

## Admission, alignment and independent economic ledger

Preserve original table order. Require unique fill IDs, positive builtin integer
prices/quantities, valid side and maker/taker roles, nondecreasing fill timestamps
and coherent send/arrival/fill times where present. Parse exact tick and fee/rebate
configuration scalars from original YAML text without binary-float inference.
Calculate maker/taker per-unit plus proportional fee and maker rebate directly
from that literal configuration and integer fill values. Saved fill fee/rebate
floats are projections to check, never exact oracle inputs.

Start flat with zero cash, costs, volumes and realized P&L. Use an independent
weighted-average acquisition-price ledger: same-side fills update the weighted
average; opposing fills realize closed quantity times price-minus-average with
the old position sign; reversal starts the residual position at the new price.
Maintain integer trade cash/turnover/volumes/counts independently. Assert the
exact identity realized + unrealized = trade cash + inventory*mark in tick units.
Currency values multiply exact rationals by the literal tick before presentation.
Do not copy production signed-cost-basis recurrence or sum rounded fill costs.

PnL.fill_count must be builtin integer, monotone, start at zero, advance only by
zero/one, cover every prefix 0..N and finish at the total fill count. The first
row of prefix k has the kth fill's timestamp for k >= 1; prefix zero retains its
recorded market timestamp. Consume fills in retained order,
never timestamp-bisect or sort same-time rows. Integer inventory/cash/volumes and
prefix counters must agree on every row, including repeated timestamps/counts.

The fixed policy must be midpoint with fresh flat Portfolio initialization.
Recorded marks must be finite exact half-integer tick values, |mark| < 2**52,
positive for open inventory (zero allowed only flat). Within these bounds the
saved binary64 mark can identify the original midpoint tick value. This is a
conditional mark input, not a proof that the correct book/causal mark was chosen.
If admission fails, stop and report the residual limitation; do not replace data.
Negative marks and negative zero are inadmissible even while flat for these fixed
midpoint cohorts. Positive zero is allowed only flat. No claim of missing-row
detection or complete market observation cadence follows from this subset.

## Field and presentation contract

Check every mandatory PortfolioSnapshot field: timestamp/currency/tick, inventory,
average cost, integer trade cash/turnover/volumes/counts, cash, costs, realized,
unrealized and gross in ticks/currency, net in currency, and current/peak exposure.
Derive current exposure from exact inventory/mark/tick and peak as the running
maximum over retained mark/fill snapshots. In this fixed engine path timer
snapshots reuse the last mark and inventory; no additional hidden exposure change
is inferred. Inventory-table rows must match corresponding timestamp/inventory/
current/peak exposure projections and original order.

Require the exact 25-column PnL Snapshot schema, not merely whichever fields happen
to be present. Int64: timestamp_ns, inventory, trade_cash_ticks, turnover_ticks,
buy_volume, sell_volume, fill_count. String (Arrow string or large_string): currency.
Float64: tick_value, mark_ticks, average_cost_ticks, trade_cash, cash,
realized_pnl_ticks, unrealized_pnl_ticks, gross_pnl_ticks, realized_pnl,
unrealized_pnl, gross_pnl, fees, rebates, net_pnl, turnover,
current_gross_exposure, peak_gross_exposure. Only average_cost_ticks is nullable,
and only when flat. Inventory requires timestamp_ns/inventory Int64 and
current_gross_exposure/peak_gross_exposure Float64, without missing columns.
No net_pnl_ticks field is implied. Preserve integer/float types without coercion.

For exact rational values with denominators containing only factors 2 and 5,
project the exact finite value to binary64. For repeating values, first round to
28 significant decimal digits, HALF_EVEN, then convert that rounded rational to
binary64. The independent renderer uses integer quotient/remainder rounding, not
production helpers or float(log10). Currency projections derive from exact
currency rationals, never rounded tick views multiplied by tick value.

This repeating-view rule assumes the standard fresh-worker default Decimal
context used by the frozen source. PR14 did not retain per-row context captures;
the audit cannot retrospectively prove those contexts or exact risk arguments.
Do not compare all repeating fields with direct float(Fraction): the earlier
closed study already identified possible double-rounding differences.

Compare packed Float64 bits, with no numeric tolerance. Exact rational zero
projects to positive zero; nonzero underflow follows binary64 sign. Flat average
cost is None, never an imputed zero. All saved numeric values must be finite.
Do not require sums of separately rounded components to equal independently
projected gross/net values bitwise. Preserve all original values and units.

## Work budget, evidence and stopping

One pass over the two fixed cohorts in order 001 then 002, with no engine runs.
Caps per cohort: 100,000 PnL rows, 20,000 fills, 65,536-bit rational numerator/
denominator, 180 s wall time; total audit 600 s; generated evidence 128 MiB.
Caps are routine bounded experiment limits, not performance/capacity goals.
The 600-second total is cumulative across the initial pass and any permitted
repair/reanalysis; a future explicitly labeled reproduction has its own budget.
Persist row-level expected/observed hashes and every mismatch, or a completed
prefix if a cap/error stops processing. Refuse existing output paths. Stop after
this pass; no automatic retry, new size/seed, selected favorable field or changed
presentation/precision/gate. Source/input/schema/admission/cap failures stop the
study and preserve the exact reason.

Preflight mathematical controls cover long accumulation/partial close, short
close/reversal, exact fees/rebates, terminating versus repeating presentation,
ties-even/signed zero/None and ordering/prefix rejection. These are auditor tests,
not new market observations. Freeze them and the audited implementation before
the first reconciliation. Report actual role, partial-close, flat/reversal and
repeating-field coverage from the fixed data; unexercised cases remain limits.

If a mismatch is independently shown to be an auditor implementation error,
at most one correctness-only repair/reanalysis of the same immutable inputs is
allowed, with the original source/results and review retained. It changes no
cohort, field, criterion, tolerance or application source. A true application
defect requires separate independent reproduction/design before a narrow fix;
never silently turn a failed audit into a pass.

Independent design review precedes outcomes and independently authored result
review checks actual ledgers, projections, hashes and coverage afterward. Publish
only a substantive valid result in a branch/draft. CI may reproduce this bounded
offline reconciliation and standard correctness checks, but no engine/resource
trial is evidence for this study. No stopped optimizer, old-gate change, licensed/
private E1 input, purchase, brokerage, access/security change, desktop, merge,
deployment or trading.
