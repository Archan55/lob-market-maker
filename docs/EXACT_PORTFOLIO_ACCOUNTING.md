# Exact cost basis and atomic Portfolio validation

Valid fills previously raised `AccountingError` after being booked. At Decimal
precision 28, buys of 100,000,000 units at 600,000,000 ticks and 200,000,000 at
600,000,001 produce an exact marked gross result of 100,000,000 ticks. The rounded
average instead implies 99,999,999.99999999999 ticks. That 1e-11 discrepancy exceeds
the backtest's 1e-12 tolerance. The short mirror fails too. Cash, inventory and
fill count have already changed when the exception occurs, and snapshot retries
also fail. This is a false accounting rejection, not an economic loss.

## Accounting contract

Let `T` be integer trade cash in ticks, `q` inventory, `C` the signed remaining
acquisition notional, and `M` a finite decimal mark. The authoritative cost pool
uses `Fraction`; partial closes release their exact proportional share. Reversals
open the residual at the new fill price. Exactly:

```
realized_ticks = T + C
unrealized_ticks = q*M - C
gross_ticks = realized_ticks + unrealized_ticks = T + q*M
```

Decimal averages and repeating realized/unrealized values remain presentation
views. They never feed back into subsequent accounting or conservation checks.
Terminating cash, fees, rebates, marks and gross/net results render exactly;
repeating views respect the caller's Decimal rounding context in a local copy.
No global precision or configured tolerance is increased. Existing public fields
and method signatures remain available. Expected public views and exact quantity,
cash and cost-basis identities retain corruption checks.

Fill, mark and timestamp updates use a shallow candidate of the current scalar,
Decimal and Fraction state. They commit only after validation and snapshot
construction succeed. Invalid/nonfinite input therefore leaves Portfolio state
unchanged. This is a Portfolio method guarantee, not whole-exchange transaction,
concurrency, persistence or recovery atomicity.

Configured maker/taker costs share the same exact finite-arithmetic helper in
the exchange and Portfolio. Explicit exchange costs are still booked once.

## Independent evidence and reproduction

Two independent reviewers authored the new unit and full-backtest regressions.
The oracle stores rational acquisition pools and integer cash with explicit
checkpoints through long/short additions, partial closes, reversals and flatness.
It verifies fees/rebates, marking, caller precision changes and rejected-operation
atomicity. Public-state corruption checks remain tested; independent review caught
and corrected early implementations that missed an edited inventory and compared
average-price changes to a notional tolerance without scaling by inventory.

The integration regression runs ordinary fixed-spread quoting, matching, risk,
reports and artifact writing on a fixed original synthetic tape. Three fills have
prices 100/101/103 (long) or 104/103/101 (short), quantities 1 or 10^15, and nonzero
maker costs/rebates. All integer notionals fit signed Int64. Their exact terminal
gross result is eight tick-notional units per quantity unit, with average cost
304/3 or 308/3. The large cases fail on the original full-backtest path.

Run after installing `.[dev]`:

```bash
python -m pytest tests/unit/test_portfolio_exact_independent.py \
  tests/unit/test_portfolio_presentation_guard_independent.py \
  tests/integration/test_exact_portfolio_backtest.py -q
python tools/reproduce_exact_accounting.py
```

The reproducer extracts pinned main `1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`
into a temporary directory and runs the exact same hash-pinned tests before and
after. It retains logs, JUnit, source copies, hashes and results in
`experiments/exact-accounting-review/`. On the reviewed dependency environment,
the original has 25 failures / 14 passes; the correction passes all 39 cases.
The four original scaled-fill failures and two large full-backtest failures must
specifically reproduce the accounting exception, preventing setup failures from
being counted as defect evidence. Each pytest subprocess also asserts and records
the imported module paths and source hashes. CI saves this original synthetic
evidence with the exact checkout and observed dependencies.

Local aggregate validation on Python 3.12.14 passes 289 tests and 90.56%
branch-aware package coverage, Ruff lint/format and strict mypy. In the same
dependency environment, the ordinary 300-event `configs/ci.yaml` backtest
produces all eight Parquet replay tables byte-identically before and after.
This checks the unchanged normal witness; it does not promise byte identity for
other histories or dependency environments.

The November 1 continuation found and repaired PR3's integer-string rendering
failure on a reachable long partial-close history. The original 39 cases remain
frozen. See [the rendering and performance review](EXACT_PORTFOLIO_PERFORMANCE.md)
for its separate before/after regression, predefined paired benchmark, complete
trial disclosure and material remaining execution cost. The 289-test validation
above describes the original October 31 head, not the final continuation head.

## Scope and compatibility

This standalone fix targets unchanged main. The affected original Portfolio is
byte-identical at PR2 `a190758c1d60fff0c9526aa4201dc4759d234639`; PR1 and PR2 heads,
their frozen studies and source remain untouched. It does not incorporate their
other pending changes. No private or licensed quant-study files are used.

Repeating Decimal views can differ in their last digits from the old rounded
recurrence. Historical artifact byte identity is not promised. Exact rational
denominators can grow with long partial-close sequences, trading arithmetic cost
for correctness. This change does not calibrate execution, establish market
advantage or reopen any recovery study.
