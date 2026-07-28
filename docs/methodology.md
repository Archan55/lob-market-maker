# Methodology

## Event semantics and ticks

The canonical event is
`(timestamp_ns, sequence_number, event_type, side, price_ticks, quantity)`.
`BID=+1` and `ASK=-1`. Side always names resting liquidity. Therefore
`TRADE/BID` is an aggressive sell and `TRADE/ASK` is an aggressive buy.

`ADD` increases displayed quantity, `CANCEL` removes it, and `TRADE` consumes
it. `RESET` clears both sides. Snapshot rows rebuild levels deterministically
after a reset. External modify messages are normalized into cancel/add because
the priority consequence cannot otherwise be represented consistently.

Prices are signed 64-bit integer ticks in the book, queues, orders, fills, and
cash-in-ticks identities. Tick size converts only at economic/reporting
boundaries. This prevents floating-point prices from becoming dictionary keys
or crossing comparisons.

## Reconstruction and validation

The reference book keeps positive quantities in separate bid and ask mappings.
Zero levels are removed. It provides top prices, spread, midpoint, microprice,
top-N depth, best imbalance, and inverse-rank-weighted multi-level imbalance.

Strict replay raises on an absent/oversized reduction or crossed add. Lenient
replay removes only available depth, counts the clamp, and rejects a crossing
add. Lenient mode is diagnostic; it does not silently claim malformed input is
clean. Full validation checks timestamps, strictly increasing sequences,
schema, event fields, nonnegative depth, and uncrossed state. The configured
mode is applied before a backtest and its issue count/diagnostics are retained
in the run artifacts.

A reusable validation result is a certificate bound to the exact raw event
stream SHA-256, validation mode, book-reconstruction setting, nonempty policy,
and event count. A backtest rejects a certificate from any different stream or
settings, preventing validation/replay drift. The selected replay stream is
fingerprinted separately in the run summary.

## Event ordering and causality

Scheduler order is:

```text
(timestamp_ns, causal_wave, phase, source_sequence, schedule_id)
```

Within one time/wave:

1. historical market event;
2. venue session/expiry control;
3. client command arrival;
4. market-data delivery;
5. execution-report delivery;
6. timer;
7. strategy decision;
8. audit.

This convention is conservative at ties. The historical event precedes a
cancel or new order arriving at the same exchange timestamp. A cancel can
therefore lose to a trade at that time; a new order cannot receive that trade.

If a handler schedules same-time work into an already reached phase, the work
moves to the next causal wave. A monotonic schedule ID gives a total order.
This makes zero configured latency causal without inventing a physical delay.

## True versus observed state

Historical events update the exchange-owned true book immediately. The market
channel carries the immutable original event, and the strategy runtime applies
it later to its own observed book. It never builds a delayed message by copying
the true book at delivery time, which would leak intervening events.

True portfolio inventory updates at exchange fill time. Strategy-known
inventory updates only when the delayed fill report arrives. Strategy contexts
are frozen book views plus known state; they contain no exchange, true book,
true portfolio, scheduler, or future-evaluation object.

Each ordered channel samples fixed or seeded-jitter latency and clamps delivery
time to its previous delivery, preserving feed/report sequence even when a
later message samples less delay.

## Order arrival and matching

Marketability is evaluated against the true book at exchange arrival, not
decision time. A post-only order that crosses then is rejected. A marketable
limit walks eligible opposite levels best-first, executes no more than
displayed quantity, respects its limit, and optionally rests a remainder.

Passive remainders and nonmarketable limits enter the queue overlay.
Cancellations remain ineffective until venue arrival. Replace is cancel/new,
not an atomic mutation. A stale cancel receives a rejection report. Allowed
order-state transitions are validated and terminal states cannot reopen.

Report order is deterministic. Acceptance precedes any immediate execution
reports at the same timestamp. Every order, transition, fill, and report has a
stable run-local ID.

## Queue approximation

See [queue_model.md](queue_model.md). The central implementation choice is one
shared segment FIFO per active side/price. One reported trade quantity walks it
once, so multiple own orders cannot each reuse the same trade volume.

At exact price, external/own segments are consumed in estimated FIFO order.
At a price-through, better-priced own liquidity is assumed swept. Cancellation
allocation is a sensitivity assumption: back, pro-rata, or front.

The external historical book remains an exogenous path while own liquidity is a
counterfactual overlay. A reset discards unobservable priority. These are
explicit Level 2 limitations, not exact matching-engine reconstruction.

## Latency records

The audit keeps:

- strategy decision timestamp;
- send timestamp;
- exchange-arrival timestamp;
- exchange-fill timestamp;
- strategy-notification timestamp.

The tested causal inequalities are:

```text
decision <= send <= arrival <= fill <= notification
```

for fields applicable to a fill. Nanosecond integers express ordering and input
resolution only.

## Accounting

For signed quantity `dq` (buy positive, sell negative):

```text
inventory += dq
trade_cash_ticks -= dq * fill_price_ticks
gross_pnl_ticks = trade_cash_ticks + inventory * mark_ticks
net_pnl = gross_pnl_ticks * tick_size - fees + rebates
```

Fee costs and rebate income are separate nonnegative ledgers. Average-cost
realized P&L handles adding, partial closes, flattening, and crossing zero.
Unrealized P&L plus realized P&L reconciles to gross marked P&L; cash plus
marked inventory reconciles to net P&L. Decimal arithmetic is used at economic
boundaries with a small documented tolerance for repeating average costs.

Default marking uses midpoint. Microprice is available; conservative marking
uses the liquidation-side best quote. Midpoint does not include liquidation
cost or market impact.

## Risk

Pre-trade worst-case long exposure adds every live/pending buy; worst-case short
exposure subtracts every live/pending sell. Opposite orders are not netted.
Checks cover position, order size, open quantity/count, spread, optional
volatility, message rate, session cutoff, and kill state.

Continuous marked P&L updates loss and drawdown state. A kill switch suppresses
new risk and requests all live cancels while allowing valid risk-reducing
actions. Quote-age checks schedule a deduplicated risk cancel at the exact
configured age boundary, independent of the strategy refresh timer. Session
cutoff actions are audited. The end policy either marks remaining inventory or
submits a simulated marketable liquidation against remaining true depth, which
may fill only partially.

## Strategy equations

The fixed strategy uses observed midpoint and constant half-spread.

The inventory-aware strategy uses:

```text
r = midpoint - inventory_penalty * (known_inventory / maximum_inventory)
```

and tapers size near the limit.

The microprice strategy uses:

```text
I = (bid_qty - ask_qty) / (bid_qty + ask_qty)
microprice = (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)
r = microprice + imbalance_coefficient * I
    - inventory_penalty * normalized_inventory
half_spread = max(minimum_half_spread,
                  base_half_spread + volatility_multiplier * rolling_volatility)
```

Rolling volatility uses only changes in delivered midpoints. Bids round down,
asks round up, and passive quotes are forced uncrossed.

## Markouts

For fill price `p` and future true midpoint `m_h`:

```text
buy markout_h  = m_h - p
sell markout_h = p - m_h
```

Positive is favorable for either side. The selected future state is the first
true midpoint at or after fill time plus 100 ms, 1 s, or 5 s. Missing future
states stay missing. Values are reported in ticks, currency per unit, and basis
points relative to fill price.

True future states enter only this completed-run calculation.

## Metrics and attribution

Activity, maker/taker volume, fill/cancel ratios, gross/net P&L, costs,
drawdown, inventory distribution, spread/depth conditions, queue estimates,
markouts, and measured throughput are derived from audit artifacts.
“Near limit” means absolute inventory at or above the configurable
`metrics.near_limit_fraction` of the position limit.

Every selected canonical stream is fingerprinted from its exact event fields.
Reports store that SHA-256, and multi-run comparison refuses different hashes,
symbols, event counts, or provenance.

Experiment manifests store the same selected-stream hash. Grid dimensions must
be finite and unique, run-directory names encode exact multiplier values, and
the CLI requires an explicit override above 100 cases. Case-study publication
validates the experiment manifest and table before mutating report outputs.

Spread capture and inventory mark-to-market attribution are analytical
approximations. Fees and rebates are exact ledgers. The attribution residual is
reported/reconciled rather than hidden. Annualized Sharpe is omitted for the
short event-time synthetic demonstration.

## Sources of bias

The principal biases are queue uncertainty, exogenous historical replay,
unmodelled response to our orders, approximate marketable depth, data
timestamp/sequence quality, hidden liquidity, venue-specific priority, cost
calibration, and survivorship/selection choices in any future dataset. Results
must be stress-tested across latency, fees, cancellation allocation, order
size, regime, and data samples before any economic interpretation.
