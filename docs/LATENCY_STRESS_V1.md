# Latency-boundary and partial-fill stress — version 1

> **SYNTHETIC MECHANICS EVIDENCE.** These constructed tapes validate replay
> semantics and accounting. They do not estimate venue latency or profitability.

The full backtest passes 50 scenarios with independently derived fill, order,
report and cash/P&L oracles. The suite holds market-data latency at **100 us**
and moves command arrivals across actual historical event boundaries. It
extends the [canonical cancellation witness](QUEUE_CANCELLATION.md) without
changing its tape, configuration, hashes, report or independent auditor.

## Reproduction and artifacts

From the repository root, after installing `.[dev]`:

```bash
python -m pytest tests/integration/test_latency_stress_v1.py -q \
  --latency-study-output experiments/latency-stress-v1
python -m pytest --cov=lobmm --cov-report=term-missing \
  --latency-study-output experiments/latency-stress-v1
```

The dedicated suite contains **63 tests**: 50 scenario checks, one complete
repeat, six comparisons with ordinary unobserved backtests, and four
trace/oracle corruption controls plus two saved-input corruption controls.
The repeat verifies **400 Parquet pairs**, all 50 saved
tapes/configurations/summaries, all 50 scheduler snapshot files, and the full
JSON audit byte for byte. Every audit re-reads the saved tape and configuration
and checks the tape checksum and input-stream fingerprint. Wall-clock diagnostics and metrics are excluded
from byte comparisons. Exact Parquet byte identity is verified within one
installed environment, not promised across serializer versions.

Each `experiments/latency-stress-v1/<case>/` contains `tape.csv`,
`run_config.yaml`, the eight normal replay tables, summary/metrics/diagnostics,
and `event_snapshots.json`. The root `audit.json` records all case outcomes,
canonical event-stream hashes and SHA-256 digests for **600 deterministic
artifacts**. The saved configuration's `input_path: tape.csv` is relative to
that case directory. Replaying with Python requires loading the tape from that
directory and passing the saved configuration to `run_backtest`.

CI runs the suite inside full coverage and uploads this evidence as
`latency-stress-v1-<commit>` for 14 days. The artifact identifies the exact
checkout; PR1 remains a draft. No matching, scheduler, portfolio or strategy
implementation changes were needed for these cases.

Local validation on Python 3.12.14: Ruff lint and formatting passed; strict mypy
passed; the full suite passed **405 tests with 91.02% branch-aware coverage**.
The 300-event CI smoke backtest and canonical nine-case experiment/audit
passed. The canonical Markdown and JSON audits reproduced byte for byte, and
their tape/configuration/auditor files remained unchanged. See PR1 for the
exact published commit and associated GitHub Actions result.

## Measured event boundaries

Own quotes are 10 units at bid 99 and ask 101. Arrival decisions originate at
100 us after both initial book messages are delivered. Entry latency is
899,999 / 900,000 / 900,001 ns, so venue arrival is 1 ms minus 1 ns / exactly
1 ms / plus 1 ns. Cancellation and report latency stay fixed in these entry
sweeps. Every result below is checked on **both sides**.

| Scenario (cases) | 1 ns before event | At event | 1 ns after event |
|---|---|---|---|
| Arrival at the canonical 1 ms add (9) | Back/pro-rata/front: **0/5/10** | **0/0/0** | **0/0/0** |
| Arrival at empty-price ADD(10), then TRADE(5), at 1 ms (9) | **5/5/5** | **0/0/0** | **0/0/0** |
| Own cancel at the canonical 3 ms trade (9) | **0/0/0** | **0/5/10** | **0/5/10** |
| Own cancel at the canonical 1 ms add (9) | No subsequent fills | No subsequent fills | No subsequent fills |

For the canonical arrival sweep, initial external 100 precedes an own quote;
later external 50 follows it. Cancelling 60 leaves external ahead **90/60/40**
under back/pro-rata/front allocation. Trading 65 therefore reaches **0/5/10**
own units. At or after the add, all 150 starts ahead, and any allocation leaves
90 ahead. The suite copies queue position at arrival and after each external
cancel to check these intermediate values directly.

The positive trade discriminator starts with an external book at 98/102,
leaving the quote prices 99/101 empty. At 1 ms, each side receives historical
ADD(10), then TRADE(5). Arrival one ns earlier rests ahead of the add and fills
five. At or later than 1 ms, both historical events execute first; the quote
joins behind the remaining external five and fills zero. This distinguishes
the scheduler's actual tie ordering even when a trivial no-fill case would
otherwise pass. In the before-event case the acceptance report triggers the
post-cutoff cancel decision at 1,099,999 ns; at/after cases wake at 1,100,000 ns.
The oracle includes this one-ns decision difference.

For trade cancellation, quotes enter at 250 us and cancellation decisions
occur at 2.1 ms. Sweeping cancel latency 899,999 / 900,000 / 900,001 ns puts
cancel arrival around the 3 ms trade. Pro-rata fills **0 / 5 / 5**, then
cancels the remaining five in the at/after cases. Front allocation fills the
entire ten at the trade, and the pending at/after cancel produces a terminal
`cancel_rejected` report with `order_not_live`; it does not mutate the filled
order. Back allocation fills zero throughout.

For cancellation around the add, the acceptance report triggers a cancellation
decision at 350 us. Cancel latency 649,999 / 650,000 / 650,001 ns moves the
arrival around 1 ms. These cases check the scheduler keys directly: both
historical adds precede both same-time cancels. All own orders terminate
before the later trades, so fill totals alone cannot establish this ordering.

## Split fills, report lag and residual marks

Pro-rata starts with 60 external ahead after the cancellation. The primary
side's trades of 63 and 2 at 3 and 3.2 ms produce own partials **3 + 2**;
the other side's trades of 62 and 2 produce **2 + 2**. The two mirrored cases
leave inventory **+1 / -1**. Both residual quotes cancel at 3.65 ms. Historical
best prices subsequently move from 99/101 to 100/104, marking the residual at
**102 ticks**. The independent raw-book oracle validates every historical
reduction and uncrossed transition, including the intervening marks.

Execution-report latency is **0 / 100 us / 2.5 ms**. Executions, order histories,
inventory and P&L tables remain byte-identical across that sweep. With 2.5 ms
reports, fills at 3 and 3.2 ms reach the strategy at 5.5 and 5.7 ms, after
session end. True cash/inventory book at execution time; known inventory moves
only on delivery. Delivery never books fees, rebates or cash again.

Eight deterministic seeds (0–7) partition each side's trade budget into four
positive trades, with one cut in external-ahead volume and two in own volume.
A scalar oracle subtracts ahead and remaining quantity once per budget; each
side generates three separate fill records. These stress asymmetric residuals,
partial-to-full transitions and terminal cancels while keeping all raw trades
within displayed historical depth.

| Case | Bid fills | Ask fills | Fill records | Residual | Final mark | Net cash + marked inventory |
|---|---:|---:|---:|---:|---:|---:|
| `split-residual__+1__report0` | 5 | 4 | 4 | 1 | 102 | $0.095601 |
| `split-residual__+1__report100000` | 5 | 4 | 4 | 1 | 102 | $0.095601 |
| `split-residual__+1__report2500000` | 5 | 4 | 4 | 1 | 102 | $0.095601 |
| `split-residual__-1__report0` | 4 | 5 | 4 | -1 | 102 | $0.055599 |
| `split-residual__-1__report100000` | 4 | 5 | 4 | -1 | 102 | $0.055599 |
| `split-residual__-1__report2500000` | 4 | 5 | 4 | -1 | 102 | $0.055599 |
| `seeded-partitions__0` | 9 | 7 | 6 | 2 | 100 | $0.134402 |
| `seeded-partitions__1` | 5 | 4 | 6 | 1 | 100 | $0.075601 |
| `seeded-partitions__2` | 3 | 5 | 6 | -2 | 100 | $0.067198 |
| `seeded-partitions__3` | 6 | 8 | 6 | -2 | 100 | $0.117598 |
| `seeded-partitions__4` | 6 | 10 | 6 | -4 | 100 | $0.134396 |
| `seeded-partitions__5` | 7 | 3 | 6 | 4 | 100 | $0.084004 |
| `seeded-partitions__6` | 4 | 3 | 6 | 1 | 100 | $0.058801 |
| `seeded-partitions__7` | 8 | 3 | 6 | 5 | 100 | $0.092405 |

## Independent accounting checks

Expected fills come from hand-derived boundary quantities or seeded scalar
budget arithmetic, never from the exchange or queue model. Expected external
book and midpoint paths use separate dictionaries, never `L2Book`. For buy
quantity `B`, sell quantity `S`, and mark `M`, the independent equal-price
identities are:

```text
inventory = B - S
trade_cash_ticks = -99*B + 101*S
gross_pnl_ticks = trade_cash_ticks + inventory*M
realized_pnl_ticks = 2*min(B, S)
net_pnl = gross_pnl_ticks*0.01 - fees + rebates
```

Each maker fill is charged 0.002 per unit plus 0.0001 of notional, and earns
0.0005 per unit in rebates. The audit checks each fill's costs, unique
execution/report/transition IDs, fill/order joins, every historical remaining
quantity, terminal lifecycle, causality and final conservation. It checks
cash, turnover, average cost, realized/unrealized P&L, fees/rebates, exposure
and inventory projections at **every fill snapshot and every market mark**,
including intermediate bid/ask states sharing a timestamp. Accounting tables
serialize decimals as floats; comparisons use a 1e-12 tolerance.

Scoped test observers call each original constructor/handler exactly once and
copy state after scheduler handlers. Their snapshots retain actual ordering
keys `(timestamp, wave, phase, source_sequence, schedule_id)`, current live
queue position, historical versus overlay external depth, true cash/inventory,
strategy-known inventory and observed sequence. Six ordinary, unobserved
backtests produce identical deterministic artifacts, checking that observation
does not change results. Corruption controls reject report-time cash booking,
early inventory knowledge, swapped tie order and a wrong fill oracle even
when final saved totals remain intact.

## Assumptions and limits

Historical-market-first tie priority is a documented model assumption verified
against actual scheduler execution, not a measured venue rule. The constructed
tapes pass strict input and exchange validation with no book diagnostics;
realizability does not make them a sampled or calibrated market dataset.
Own execution displaces external volume in the counterfactual queue overlay,
while the historical book remains exogenous. Their external-depth divergence
is an expected replay assumption, not evidence of lost or duplicated volume.

The existing `queue_ahead_estimate` and cumulative fields in saved transition
rows repeat final state. They are not used as event-time trajectory evidence.
The new snapshots are copied after each scheduler handler; they do not expose
internal substeps within a handler. Intermediate accounting comes from the
separately audited per-fill and per-market snapshots.

This version uses fixed latency, FIFO matching, passive quotes, midpoint marks,
a mark-at-session-end policy and single-process pytest observers. It does not
cover jitter, every possible tape, calibrated Level 3 priority, market impact,
or alternative marking/liquidation policies. Seeded partitions extend finite
conservation coverage; they are not a proof for all event streams. Nanosecond
boundaries express causal tests, not wall-clock performance. These cases found
no proven engine defect requiring an implementation fix.
