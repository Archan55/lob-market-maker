# Modelling assumptions

This file records assumptions that materially affect interpretation. Defaults
are conservative where a unique truth is unavailable, and sensitivity choices
are configurable.

## Data and event semantics

- Input is a correctly sequenced single-venue Level 2 stream after adapter
  normalization. Timestamps are nondecreasing; sequence numbers are strictly
  increasing globally and deterministically order equal timestamps.
- `side` always names resting liquidity. `TRADE/BID` is an aggressive sell;
  `TRADE/ASK` is an aggressive buy.
- A dataset `MODIFY` is mapped to cancel/add because priority consequences
  depend on venue rules that the canonical format cannot infer.
- Reset/snapshot data is represented as a reset followed by levels in stable
  side/price order. Resting simulated orders are re-anchored or cancelled
  according to exchange configuration; reset behavior is reported.
- Prices and quantities are already adjusted into configured ticks and lots.
  Corporate actions, roll logic, auction rules, hidden liquidity, pegging, and
  venue-specific priority are outside the current scope.

## Historical replay and market impact

- Historical market events remain an exogenous path. Our resting liquidity is a
  queue overlay and does not cause the historical market to react.
- Consequently, a simulated fill can change which queue volume would have been
  consumed in reality while subsequent historical L2 updates still follow the
  recorded path. This counterfactual inconsistency is unavoidable without a
  market-response model and is diagnosed rather than hidden.
- Marketable simulated orders walk displayed historical depth at arrival. This
  mutates the simulator's current executable depth, but future recorded updates
  did not anticipate our trade; later malformed reductions may therefore
  require lenient clamping. Marketable results are approximate.
- There is no self-trade matching against our own resting orders in the current
  implementation; self-trade prevention rejects or skips such interaction.
- Large orders can invalidate the no-market-impact assumption. Demonstration
  sizes are deliberately small.

## Queue position

Level 2 reveals aggregate depth, not individual order identities, priority,
hidden quantity, or which order cancelled. Queue position is therefore an
estimate.

- An order arriving at an occupied price joins behind currently displayed
  external depth and all earlier own orders at that price.
- Later displayed adds join behind our resting orders.
- Exact-price trade quantity walks one shared FIFO overlay once. External
  quantity ahead is consumed before our order; eligible remainder may
  partially or fully fill it.
- A trade printed through a resting price is assumed to have swept all eligible
  own quantity at the better price. This is explicit price-through logic.
- `back_of_queue` cancellation removes external volume behind our order first
  and is the conservative baseline.
- `front_of_queue` removes external volume ahead first and is optimistic.
- `pro_rata` allocates cancellation among external segments in proportion to
  estimated size with stable integer rounding.
- Resets lose queue observability. The default cancels/re-anchors simulated
  queue estimates rather than pretending priority survived.

These choices are sensitivity cases, not claims about an actual exchange.

## Time, ordering, and latency

- Timestamps are nanosecond integers for ordering; they do not imply the Python
  engine processes at nanosecond speed.
- At an identical exchange timestamp, recorded market activity occurs before a
  client command. This deliberately prevents reacting to and trading ahead of
  the same event.
- A cancellation sent earlier but arriving at exactly the trade timestamp loses
  to that historical trade.
- Causal waves prevent zero-latency reactions from being scheduled backward
  into an already completed phase.
- Fixed latency is the baseline. Optional seeded jitter is a research
  sensitivity, and per-channel delivery is clamped to preserve message order.
- Clock synchronization errors, packet loss, exchange batching, feed
  arbitration, and hardware/network tail behavior are not inferred from L2
  data.

## Strategy knowledge

- The strategy sees only its observed book, delivered acknowledgements and
  fills, delayed known inventory/account state, and timers.
- Known inventory changes on fill notification, not exchange fill time.
- Open-order shadow state can be stale between exchange action and report.
- Rolling volatility and imbalance use only delivered observations at or
  before decision time.
- Warm-up decisions are suppressed until configured history is available.

## Matching and orders

- Price-time priority is the reference priority rule.
- Marketable limit orders execute at displayed opposite-side prices up to their
  limit and available quantity. An allowed remainder rests at its limit.
- Post-only orders that would cross at venue arrival are rejected.
- Replace is ordered cancel/new and not atomic.
- Cancels are effective only at exchange arrival. Stale cancel requests produce
  an auditable rejection.
- Partial fills preserve original quantity identity, and fill quantity never
  exceeds remaining order quantity.
- One strategy-owned quote per side is a deliberate invariant of the current
  quote manager, not a configurable multi-order mode.
- Venue auctions, stop orders, IOC/FOK/AON, reserve/iceberg replenishment, and
  complex time-in-force rules are not modeled.

## Risk controls

- Pre-trade limits use projected worst-case exposure from live and pending
  orders; opposite sides are not netted.
- Quote-age, session, loss/drawdown, and kill-switch checks request deduplicated
  cancellations only for actual live orders.
- A stale venue cancel remains a separately auditable rejection and never
  rewrites prior order state.
- Session end either marks remaining inventory or submits an approximate
  marketable liquidation that may fill only partially.

## Fees, rebates, and accounting

- Fee costs and rebate income are configured and stored as separate
  nonnegative amounts. A positive fee is a cost; a positive rebate is income.
- Tick size is the currency value of one price tick per unit. Proportional fees
  are assessed on currency notional.
- Cash books executions and costs immediately at exchange fill time. Strategy
  knowledge remains delayed.
- Default marking is true midpoint for ex-post accounting. Microprice and
  conservative liquidation marks are sensitivity choices.
- Realized P&L uses average cost. Gross marked P&L equals trade cash plus marked
  inventory; net P&L equals gross P&L minus fees plus rebates.
- The simulator does not model funding, borrow, margin interest, taxes, FX
  conversion, settlement, or capital charges.

## Markouts and metrics

- Side-adjusted markout is future midpoint minus fill price for buys and fill
  price minus future midpoint for sells.
- The future mark is the first true midpoint at or after each requested horizon.
  If none exists, the markout is missing rather than extrapolated.
- Markouts are ex-post only and cannot be imported into strategy modules.
- Attribution components are analytical approximations and report a residual
  that reconciles to net P&L within numeric tolerance.
- Sharpe is omitted for short or unsuitable samples; no annualization is
  implied by event-time P&L.
- Throughput is measured on the actual environment and selected deterministic
  dataset. It is never inferred from timestamp precision.

## Synthetic data

- Synthetic events exist to exercise correctness, reproducibility, changing
  depth/spread, volatility regimes, and order-flow regimes.
- They do not replicate a calibrated exchange, strategic agent response,
  hidden liquidity, real clustering, or real latency.
- Synthetic P&L, markouts, fill rates, and strategy comparisons are
  demonstrations only and must not be described as expected live performance.
