# Limitations

This simulator is a research tool. Its outputs are conditional on data,
configuration, and modelling assumptions—not predictions or trading advice.

## Level 2 and queue observability

Level 2 data cannot reveal exact individual queue position. It aggregates
orders at a price and generally cannot identify which order cancelled, hidden
or reserve quantity, participant priority, or order-level modification.
`back_of_queue`, `pro_rata`, and `front_of_queue` cancellation policies are
sensitivity assumptions rather than observed truth.

## Counterfactual market response

Historical replay cannot fully model the market's reaction to our orders.
Recorded traders did not see the simulated quote, did not change routing, and
did not respond to its fill. The strategy's passive liquidity is an overlay on
an exogenous recorded path.

Large simulated orders may invalidate the assumption that the strategy has no
market impact. Demonstration sizes are deliberately small, but no universal
safe size can be inferred from Level 2 depth alone.

## Marketable execution

Marketable limits consume eligible displayed historical depth at exchange
arrival. That execution is approximate: hidden liquidity, matching priority,
race outcomes, response/replenishment, and later recorded updates are not
counterfactually regenerated. A partial session liquidation can leave
inventory.

## Feed and venue detail

Correct results depend on timestamp semantics, sequence completeness, side
mapping, trade/cancel meaning, snapshots, tick/lot changes, and session rules.
The canonical schema cannot express every venue's auctions, implied orders,
pegging, pro-rata priority, self-trade policy, reserve replenishment, or
multi-feed arbitration. A real adapter needs venue-specific validation.

## Latency

Fixed or seeded-jitter latency is not a full network/venue latency model. It
does not infer hardware queues, packet loss, feed gaps, clock error, matching
engine batching, or correlated tail latency. Nanosecond timestamp storage is
not a claim of nanosecond processing performance.

## Accounting and costs

Default midpoint marking may be optimistic for liquidation, especially in wide
or thin markets. The current v0.2 scope omits funding, borrow, margin, taxes,
settlement, and FX conversion. Fee and rebate schedules must be calibrated to
the instrument, tier, and period; results can change materially after costs.

P&L attribution between spread capture, inventory movement, and adverse
selection is an analytical approximation. Only total ledger identities and
explicit costs/rebates have a direct accounting definition.

## Markouts and statistics

Forward markouts use the first true midpoint at or after a horizon, so sparse
data can shift the realized evaluation time. Missing future marks are not
extrapolated. Event-time observations are dependent and irregular.

An annualized Sharpe ratio is not reported for the synthetic demonstration
because its return sampling and annualization would be inappropriate. Any
future statistical claim needs adequate out-of-sample data, multiple regimes,
and explicit uncertainty.

## Synthetic data

Synthetic data is not proof of profitability. It is deterministic test and
demonstration input with changing depth, spread, volatility, and order-flow
regimes. It is not calibrated to reproduce strategic behavior, real queue
dynamics, hidden liquidity, venue microbursts, or empirical tails.

Results are sensitive to fees, rebates, latency, fill assumptions,
cancellation allocation, order size, session policy, and data quality.

Past backtest performance does not guarantee future results.
