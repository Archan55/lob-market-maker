# Results and interpretation

No metric is manually invented. Committed demonstration figures and measured
throughput are reproducibly generated from the recorded configuration, event
stream, and environment. They must be interpreted with that context.

For the provided synthetic stream, every output must be labelled
**synthetic demonstration**. It verifies system behavior and permits
side-by-side mechanics; it is not evidence of expected market profitability.

## Run outputs

`summary.json` contains the run classification, dataset provenance, exact
event-stream SHA-256, event count, compact outcome, and disclaimer.
`diagnostics.json` records the hash-bound validation certificate and any input
repairs, desired/submitted/retry-suppressed/risk-blocked/exchange-rejected
action counts, risk-originated cancels, scheduler activity, wall time, and
optional traced peak memory.
`metrics.json` groups activity, P&L, inventory, execution quality, market
conditions, risk, approximate attribution, and engineering statistics.

Parquet tables retain the auditable detail:

- `orders`: every order-state transition and timing;
- `fills`: side, price, quantity, maker/taker role, cost, and causal times;
- `inventory` and `pnl`: marked account path;
- `quotes`: submitted/cancelled strategy intent and observed sequence;
- `risk_events`: pre-trade and continuous risk decisions, including quote age
  and kill-switch events; venue rejections are counted separately in
  diagnostics;
- `market`: true ex-post market path;
- `markouts`: fill/horizon as-of marks and side-adjusted outcomes.

## Reading a comparison

Comparison requires matching symbol, provenance, event count, and exact
event-stream SHA-256; mismatched runs fail instead of producing a misleading
chart. Change only the intended strategy configuration and inspect, in order:

1. whether risk and end-inventory behavior are comparable;
2. whether fill volume and maker/taker mix changed;
3. fees/rebates and message intensity;
4. gross versus net P&L identities;
5. markouts by horizon/side/regime;
6. sensitivity to queue cancellation and latency;
7. missing-mark and data-quality diagnostics.

A higher synthetic net P&L alone is not a meaningful selection criterion.

## Attribution

Spread capture is computed relative to a contemporaneous true midpoint, costs
and rebates come from the ledger, and inventory mark-to-market is used as a
reconciliation component. These labels are useful diagnostics but not a unique
economic decomposition. The reported residual must reconcile to total net P&L
within numeric tolerance.

## Sensitivity experiments

An experiment directory contains `sensitivity.csv`,
`sensitivity.parquet`, `experiment.json`, `REPORT.md`, and an overview plot.
The manifest records the exact Cartesian grid, case count, classification, and
canonical event-stream SHA-256. Run names encode exact multiplier values, so
nearby parameter values cannot silently overwrite each other. Duplicate
dimension values are rejected, and the CLI requires `--allow-large-grid` before
executing more than 100 cases. Case-study publication verifies the manifest,
table row count, classification, event count, unique run names, and stream hash
before copying any experiment result.

## Benchmarks

`benchmark` is a profiled and traced reference-book replay.
`benchmark-full` is the repeated, unprofiled causal simulator loop with a
separate memory pass. Copy a number into a portfolio or résumé only from an
actual run whose command, scope, repeat statistic, event hash/provenance,
Python version, and hardware are disclosed. See [performance](performance.md).
