# Event-Driven Limit-Order-Book Market-Making Simulator

## Status and scope

`lob-market-maker` is a research-grade Python repository for deterministic,
historical replay of one instrument from canonical Level 2 market events. Its
importable package is `lobmm`.

The first release is intentionally limited to:

- one instrument and one venue per run;
- aggregated Level 2 events;
- historical and deterministic synthetic data;
- limit orders, cancellations, normalized cancel/new replacements, partial
  fills, fees, rebates, risk, metrics, and reports;
- fixed-spread, inventory-aware, and microprice/order-flow strategies.

It is not a live trading system. Live feeds, exchange or broker connectivity,
credentials, automatic real-money submission, machine learning, reinforcement
learning, multi-asset logic, and web dashboards are explicitly out of scope.

## Research guarantees

1. Strategy decisions use only delayed market-data messages, delayed reports,
   strategy timers, and strategy-known account state.
2. The true exchange book is never passed to strategy code.
3. Future midpoints are used only by post-run markout analysis.
4. Prices are integer ticks in matching, queueing, and accounting records.
5. Every scheduled event has a deterministic total ordering.
6. A submitted order cannot fill before exchange arrival, and a cancellation is
   ineffective until its exchange arrival.
7. Randomness is owned by explicitly seeded generators.
8. Synthetic results are demonstrations, not evidence of profitability.

## Canonical market event

Each event contains:

| Field | Type | Meaning |
|---|---|---|
| `timestamp_ns` | signed 64-bit integer | Exchange event timestamp, nonnegative |
| `sequence_number` | signed 64-bit integer | Deterministic feed order |
| `event_type` | enum | `ADD`, `CANCEL`, `TRADE`, `RESET`, or `SNAPSHOT` |
| `side` | nullable enum | Resting side, `BID=+1`, `ASK=-1` |
| `price_ticks` | signed 64-bit integer | Integer price; positive where applicable |
| `quantity` | signed 64-bit integer | Positive where applicable |

For `ADD` and `CANCEL`, `side` is the displayed resting-book side. For
`TRADE`, `side` is the resting side consumed: `TRADE/BID` therefore means an
aggressive seller consumed bid liquidity. `RESET` may omit side, price, and
quantity. A snapshot is represented by a reset followed by deterministic
snapshot-level events. External `MODIFY` records are normalized to cancel/add
by dataset adapters.

Canonical validation checks timestamp and sequence ordering, event fields,
positive prices and quantities where required, valid enums, deterministic
equal-timestamp order, nonnegative depth, and uncrossed state.

## Aggregated book

The reference book stores `dict[int, int]` quantities separately for bids and
asks. It supports add, cancel, trade, reset/snapshot, best prices, spread,
midpoint, microprice, top-N depth, quantity lookup, best-level imbalance, and
weighted multi-level imbalance.

- Strict mode raises a descriptive error for impossible events.
- Lenient mode clamps malformed reductions, increments diagnostics, and keeps
  the book usable.
- Empty levels are deleted and every stored quantity is positive.
- A valid uncrossed book has `best_bid < best_ask`.

## True state, observed state, and latency

The exchange owns the true book and applies historical events at their actual
timestamps. Each immutable event message is delivered to the strategy runtime
after market-data latency and then applied to a separate observed book. The
message contains the original event, never a late copy of current true state.

Orders, cancels, market data, and reports have independently configured fixed
latencies, with optional seeded jitter. Each ordered channel clamps delivery
timestamps to preserve source order.

Recorded times include decision, send, exchange arrival, exchange fill, and
strategy notification timestamps. Nanosecond representation is not a
performance claim.

## Scheduler ordering

The scheduler heap key is:

`(timestamp_ns, causal_wave, phase, source_sequence, schedule_id)`.

Phases at one time and wave are:

1. historical market event;
2. venue expiry/session control;
3. order or cancellation arrival;
4. market-data delivery;
5. execution-report delivery;
6. timer;
7. coalesced strategy decision;
8. audit snapshot.

Historical events therefore beat commands with the same timestamp. A trade can
fill an order whose cancel reaches the venue at that same timestamp, while an
order reacting at that timestamp cannot receive the already processed trade.
If a handler schedules an event at its current timestamp into an equal or
earlier phase, the scheduler increments `causal_wave`. A monotonic
`schedule_id` supplies a final total-order tie-break.

## Orders and exchange

Immutable request/report models are separated from mutable exchange order
records. Valid order states are:

`PENDING_ARRIVAL`, `LIVE`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`,
`EXPIRED`, and `REJECTED`.

Terminal states cannot transition. `LIVE` may partially fill, fully fill,
cancel, or expire. `PARTIALLY_FILLED` may fill, cancel, or expire. A replace is
normalized into cancel then new and is not atomic. Stale cancels yield a
rejection report without mutating a terminal order.

At arrival, a marketable limit order walks eligible opposite-side depth in
price-time order, never beyond its limit or available displayed quantity.
Post-only crossing orders are rejected. An allowed unfilled remainder rests at
its limit. Historical replay is exogenous; marketable counterfactual execution
is an approximation documented in `docs/limitations.md`.

## Level 2 queue approximation

For each active `(side, price)`, the queue model owns one shared FIFO sequence
of external and own-order segments.

- The first own order starts behind displayed external depth.
- Later own orders append behind all existing segments.
- Later external adds append behind existing own orders.
- An exact-price historical trade walks the FIFO once with one quantity budget.
- Trade volume consumes external volume ahead before filling own segments.
- Price-through events fully fill eligible better-priced own orders under the
  configured baseline assumption.
- Removing an earlier own order reduces the derived queue ahead of later orders.

External cancellation allocation is configurable:

- `back_of_queue`: remove external segments from the tail first;
- `front_of_queue`: remove them from the head first;
- `pro_rata`: allocate by external segment size with stable integer remainder
  handling.

`queue_ahead` and `volume_behind` are derived values and cannot be negative.
Exact individual order position is unobservable in Level 2 data, so all three
models are sensitivity assumptions.

## Accounting and economics

Fill quantities are signed for accounting: buy positive, sell negative. With
integer tick notional:

```text
inventory += signed_quantity
trade_cash_ticks -= signed_quantity * fill_price_ticks
cash = trade_cash - fees + rebates
gross_pnl(mark) = trade_cash + inventory * mark
net_pnl(mark) = gross_pnl - fees + rebates
```

Fee costs and rebate income are separate nonnegative fields. Maker fee, maker
rebate, taker fee, optional proportional fee, tick size, lot size, and currency
are configured. Realized/unrealized average-cost accounting handles adding,
partial closes, flattening, and crossing through zero. Default marking uses
midpoint; microprice and conservative liquidation value are supported.

After every fill, inventory, cash, remaining quantity, and gross/net P&L
identities are checked within documented numeric tolerance.

## Risk

Pre-trade checks cover maximum order size, worst-case long and short position,
open quantity, open order count, spread, optional volatility, optional message
rate, and kill-switch state. Continuous checks cover maximum loss, drawdown,
quote age, and end-of-session rules.

Worst-case projected exposure does not net live bids against live asks.
Risk-reducing actions remain permitted where safe. Activation of the kill
switch suppresses new risk and requests cancellation of live orders. Every
rejection carries a stable reason code.

## Strategies

All strategies share deterministic quote management: at most one bid and ask
by default, minimum quote lifetime, refresh interval, price/quantity thresholds,
stale cancellation, optional post-only behavior, no unnecessary duplicate
messages, and risk-side suppression.

- **Fixed spread:** midpoint fair value and fixed tick half-spread.
- **Inventory aware:** midpoint reservation price shifted against normalized
  inventory; size tapers near limits.
- **Microprice/order flow:** microprice plus imbalance signal minus inventory
  penalty, with a backward-looking rolling volatility spread adjustment.

Bids round down and asks round up. Strategy state includes only delivered data
and reports, including delayed known inventory.

## Backtest outputs

Each `runs/<run_name>/` contains:

- `run_config.yaml`, `summary.json`, `metrics.json`, `diagnostics.json`;
- `orders.parquet`, `fills.parquet`, `inventory.parquet`, `pnl.parquet`,
  `quotes.parquet`, `risk_events.parquet`, and market-state/markout tables;
- `plots/` with run-independent Matplotlib outputs.

The audit trail records all transitions, messages, times, queue estimates,
liquidity role, costs, knowledge watermarks, and risk decisions.

## Evaluation

Post-run metrics cover trading activity, gross/net accounting, inventory,
execution quality, market regimes, message activity, latency, and measured
engineering throughput. Forward side-adjusted markouts use the first true
midpoint at or after 100 ms, 1 s, and 5 s; they never feed strategies.

Annualized Sharpe is omitted unless sampling and annualization are defensible
and explicitly disclosed. P&L attribution is analytical, reconciles within a
reported residual tolerance, and is not presented as unique economic truth.

## Acceptance

The authoritative implementation checklist is `PLAN.md`. Completion requires
successful installation, Ruff, formatting, mypy, tests and coverage, all three
demo backtests, structured outputs, comparison and report generation, measured
benchmarking, verified README commands, CI consistency, and documented
assumptions without fabricated performance claims.
