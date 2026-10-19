# Reservation-to-close exposure and accounting stress — version 1

**SYNTHETIC MECHANICS EVIDENCE.** Constructed, uncalibrated tapes test risk
admission, conservation and valuation. Their P&L is an accounting outcome,
not evidence of real-market profitability.

This increment depends on PR #1 at
`d4104b70660976c6d59fe8915a75ac5616bdecd3`. It preserves the canonical
cancellation tape, configuration, auditor, Markdown/JSON reports and the
latency-stress-v1 harness. The new question is whether reservations remain
sound from order send through fills, cancellation, shutdown and final marking.

## Findings and behavior changes

Ordinary strategy orders obey the hard inventory cap in every observed state
of 27 full replays. An independent ledger transfers pending reservations to
live orders at arrival, subtracts executions, and releases residuals only at
venue cancellation or forced expiry. At each scheduler event, the audit
enumerates every hypothetical partial-fill combination of the tiny live and
pending orders. Opposing orders do not provide headroom. Delayed known
inventory differs from true inventory by up to four units; it cannot replace
the authoritative inventory used by the local gate.

Two implementation defects were demonstrated and minimally repaired:

1. **Conservative valuation used pre-fill inventory.** A fill opening or
   reversing a position could use midpoint or the wrong liquidation side in
   its immediate snapshot. Available liquidation-side depth was also ignored
   when the opposite side was absent. The backtest now selects marks from
   post-fill inventory and uses the available conservative side on one-sided
   books. A long taker opening of two units at 101 previously marked at 100.5
   with net P&L **-$0.016202**; the correct executable bid mark is 100 with net
   **-$0.026202**. A $0.02 maximum loss now triggers at execution time 8 ns,
   rather than at the later market event at 20 ns. A long-to-short maker flip
   previously overstated immediate gross P&L by two tick-notional units;
   realized P&L remains unchanged. With the opposite side absent, a partial
   long liquidation previously marked residual three units at stale 100;
   the available bid 98 yields net **-$0.070700**. Mirrored short regressions
   cover the same errors.
2. **Reduce-only admission ignored aggregate outstanding reductions.** At
   inventory +2 and position limit 2, two outstanding sells of two units and
   another proposed sell of two were classified as safe reduction; together
   they could reach -4. The public RiskManager gate now requires the proposed
   quantity plus all outstanding same-side remainder to fit current inventory.
   Opposite orders supply no capacity. Safe reductions remain admissible
   above the ordinary position cap and during a sticky kill switch.

The second fix affects caller-side `RiskManager.reduce_only` admission.
Ordinary strategy replay does not invoke that API flag, and session
liquidation bypasses it after expiring quotes. `NewOrderRequest` has no
venue-enforced reduce-only attribute. The guarantee requires callers to
supply complete authoritative outstanding reservations; it cannot constrain
omitted orders or future unrelated flow. This distinction prevents claiming
that the dormant API defect caused the ordinary replay to exceed its cap.

`tools/reproduce_exposure_defects.py` loads pinned pre-fix source in isolated
subprocesses without changing the checkout. It demonstrates **8 failing / 2
passing conservative-mark cases** and **14 failing / 2 passing reduce-only
cases** against the original head. All **26 pass after the fixes**. The
reduce-only suite independently enumerates partial-unit outcomes in **2,592
admission contexts**, including live, pending-entry and pending-cancel orders.

## What the controls establish

The initial book is bid 99 / ask 101 with two external units on each side.
Own four-unit quotes arrive before six units are added behind at 1 ms. A
four-unit historical trade at 2 ms consumes external two then own two; another
two-unit trade at 2.2 ms fills the residual. A six-unit opposite trade at 3 ms
consumes external two then own four. Each raw trade also fits recorded depth.

| Family | Cases | Independent result |
|---|---:|---|
| Position headroom and report lag | 6 | Mirrored partials 2+2 followed by opposite 4; report delay 0 / 100 us / 5 ms; no reachable cap breach. With prompt reports, a proposed new four-unit risk-increasing quote at inventory +/-4 is rejected against cap 6. |
| Refresh cancellation race | 2 | Cancels sent at 1.1 ms arrive at 2.6 ms. Primary partials win; opposite quote cancels before 3 ms. Pending-cancel quantity remains reserved throughout the race. |
| Loss shutdown with in-flight entry | 2 | Loss crosses $0.03 at 2.2 ms. A pre-approved replacement sent at 2.1 ms still arrives at 2.4 ms; true inventory remains +/-4 and reachable exposure extends to +/-8, exactly its reserved cap. |
| Aggregate open quantity | 2 | A four-unit aggregate limit rejects the second initial quote even though it is opposite-side. |
| Open order count | 2 | A one-order limit rejects the second initial quote. Stable bid-first gate ordering creates directional asymmetry; it is not a performance result. |
| Entry after session end | 1 | Both pending entries reach the closed session at 8.1 ms and are rejected without venue orders or fills. |
| Terminal marking and liquidation | 12 | Both signs, midpoint / microprice / conservative, MARK / LIQUIDATE. Inventory four meets only one executable unit at best price; liquidation leaves three despite deeper liquidity. |

The shutdown result is a **model/control limitation**, not a position-limit
defect: the kill switch suppresses future sends and requests asynchronous
live cancels. It does not revoke already in-flight requests. This study checks
the first loss trigger against the independently reconstructed P&L checkpoint,
including its complete scheduler key, verifies its reason/timestamp, and
requires the kill to remain sticky. The later entry is canceled by the next
continuous timer check. A loss threshold is not a bound on subsequent loss
while reservations remain executable.

Six complementary full replays cover all three built-in strategies and both
inventory signs with the same independent reservation and unit-fill subset
oracle. Inventory penalty, imbalance and volatility coefficients are held at
zero to control the fill witness; inventory scaling and microprice quoting
still execute. A desired size can exceed remaining headroom and be rejected;
the gate does not resize the strategy's order.

## Terminal economics

The terminal cases have one maker entry of four units at 99 (long) or 101
(short). At session end, only one external unit remains at that same best
price, with 20 at the deeper 97 bid or 103 ask. Liquidation submits the
best-price limit for four units, consumes one, and expires the unfilled three.
It deliberately cannot sweep the deeper level beyond its limit.

| Position | Policy / mark | Remaining | Gross realized ticks | Final mark ticks | Net USD |
|---|---|---:|---:|---:|---:|
| Long | MARK / midpoint | 4 | 0 | 100 | 0.033604 |
| Long | MARK / microprice | 4 | 0 | 893/9 | 0.002492888889 |
| Long | MARK / conservative | 4 | 0 | 99 | -0.006396 |
| Long | LIQUIDATE / midpoint | 3 | 0 | 99 | -0.009495 |
| Long | LIQUIDATE / microprice | 3 | 0 | 699/7 | 0.016219285714 |
| Long | LIQUIDATE / conservative | 3 | 0 | 97 | -0.069495 |
| Short | MARK / midpoint | -4 | 0 | 100 | 0.033596 |
| Short | MARK / microprice | -4 | 0 | 907/9 | 0.002484888889 |
| Short | MARK / conservative | -4 | 0 | 101 | -0.006404 |
| Short | LIQUIDATE / midpoint | -3 | 0 | 101 | -0.009505 |
| Short | LIQUIDATE / microprice | -3 | 0 | 701/7 | 0.016209285714 |
| Short | LIQUIDATE / conservative | -3 | 0 | 103 | -0.069505 |

Realized gross P&L is **zero in every terminal case**: the closing unit trades
at the original entry price. The remaining P&L is marked residual inventory
less fees plus rebates. Maker cost is 0.002 per unit plus 0.0001 of notional,
rebate is 0.0005 per unit, and taker cost is 0.003 per unit plus proportional
fees. These values are assumed test economics, not a real fee schedule.

The tape ends at **6 ms**. The closing order executes at **6.3 ms**, and
reports drain later. `end_inventory` and public `end_of_session` metrics mean
inventory after the full scheduler drain, not inventory at the last market
timestamp. The JSON audit records tape end, last economic snapshot and final
drain time separately. Midpoint and microprice marks of the three-unit
residual need not be realizable closing prices.

## Independent audit and reproducibility

The main suite has **43 tests**: 27 cases, one full repeat, seven comparisons
with unobserved production replay, and eight corruption controls. It checks
hand-derived fill budgets; request-to-order joins; unique executions and
reports; per-order quantity conservation; terminal lifecycles; actual
scheduler order; independently retained live/pending reservations; every
gate input; all hypothetical partial-fill outcomes; execution-time cash;
delayed fill knowledge; fee/rebate booking; and every accounting snapshot.

The accounting oracle reconstructs the true book with raw price/quantity
dictionaries. A rational signed cost pool removes average-cost *notional* on
closes. Realized ticks derive from `trade_cash_ticks + remaining_cost_pool`;
unrealized ticks derive from `inventory * mark - remaining_cost_pool`. This
avoids calling Portfolio or repeating its price-difference realized-P&L
accumulator. Every configured mark is derived from independent book prices
and sizes. Float artifact values use a 1e-12 tolerance; pool arithmetic is exact.
Public P&L/activity/inventory metrics reconcile to the independent ledger.

Corruption controls reject premature release at cancel send, omission/netting
of opposite reservations, report-time cash booking, early fill knowledge,
changed saved tape/configuration, wrong public net P&L and wrong realized P&L.
The observers call each original handler/send/gate exactly once and copy
state; ordinary unobserved replays verify their lack of side effects.

Each case saves its CSV, complete configuration, eight Parquet tables,
summary/metrics/diagnostics and scheduler snapshots. The complete repeat checks
**216 Parquet pairs** and **378 deterministic artifact hashes**, plus the
root audit, byte for byte within the installed dependency environment. Only
wall-clock seconds, throughput and peak traced memory are excluded from
normalized metrics/diagnostics hashes. The replay still saves those original
measurements. The 27 cases contain **281 accounting checkpoints** and **6,545
distinct reachable-inventory checks**; the latter count deduplicates inventory
values after enumerating all partial-fill vectors.

The checked-in [JSON audit](EXPOSURE_SESSION_STRESS_V1.json) records local
measurements and artifact hashes. CI uploads complete raw evidence and
before/after defect logs for 14 days and records its exact checkout plus
observed dependencies. Serializer-byte identity is not promised across
dependency versions. Existing saved transition rows repeat final cumulative
fields; event trajectories here come from independently audited snapshots.

Local validation on Python 3.12.14 passed **480 tests with 91.09% branch-aware
package coverage**, Ruff lint/format and strict mypy. The pinned-source
before/after reproduction passed. The 300-event CI replay, three-strategy
same-stream comparison/report, canonical nine-case experiment/audit and
byte-identical canonical Markdown/JSON reproduction passed. The CI workflow
checks out the actual PR head explicitly; published run and artifact links
are recorded in the draft PR once that exact-head run completes.

Reproduce from the repository root after installing `.[dev]`:

```bash
python -m pytest tests/integration/test_exposure_session_stress_v1.py -q \
  --exposure-study-output experiments/exposure-session-stress-v1
python -m pytest tests/integration/test_strategy_exposure_limits.py -q
python tools/reproduce_exposure_defects.py
python -m ruff check .
python -m ruff format --check .
python -m mypy src/lobmm
python -m pytest --cov=lobmm --cov-report=term-missing \
  --latency-study-output experiments/latency-stress-v1 \
  --exposure-study-output experiments/exposure-session-stress-v1
python -m lobmm.cli backtest --config configs/ci.yaml --run-name exposure-ci-smoke
python -m lobmm.cli experiment --config configs/queue_cancellation.yaml \
  --name exposure-queue-cancellation --latency-multipliers 0.5,1,2
python -m lobmm.cli audit-queue-study \
  --experiment experiments/exposure-queue-cancellation \
  --config configs/queue_cancellation.yaml
```

The regression reproduction helper requires the pinned baseline commit in
local Git history; CI fetches full history. Each saved `input_path: tape.csv`
is relative to its case directory. Replay with Python by loading that CSV
and passing the saved configuration to `run_backtest`.

## Preserved assumptions, limits and next step

Session termination **forces expiry at the venue** before same-time client
commands, independently of cancellation latency; this is stronger than a
client stopping its process or losing connectivity. The study verifies that
model and does not claim live orders would vanish on a real client shutdown.
Historical-market-first scheduler priority is unchanged. Passive own fills
displace overlay external volume while recorded historical depth remains
exogenous; marketable liquidation mutates executable depth after the tape.

When the required liquidation-side quote itself is absent, the backtest still
falls back to the last valid mark or fill price. Conservation alone cannot
establish that this value is executable. One-sided available-side corrections
are covered by the separate ten-case regression file; the main 27-case grid
keeps both sides populated. Neither marking nor LIQUIDATE promises flatness.

This finite suite uses fixed latency, small quantities, one instrument,
ordinary quote ownership and approximate L2 FIFO execution. It does not prove
all event streams, correlated jitter, venue-enforced reductions, dynamic
margin, calibrated liquidation costs, or empirical loss tails.

The practical next research step is a separately versioned **client-stop
policy with exchange orders surviving until explicit cancel acknowledgment**,
including pending-entry revocation and terminal mark availability. That would
change session semantics and must preserve the present forced-expiry witness.
Review the minimal fixes and these assumptions before undertaking that model
extension. No merge, deployment or trading action is part of this work.
