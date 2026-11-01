# Exact-accounting rendering regression and bounded performance review

The November 1 review found and repaired a regression introduced by PR3
`116a8618e4fe5157597015e3bf08f793ac7b720b`. It also measured a material remaining
execution cost. PR3 stays draft. These are engineering results on original
synthetic inputs already inspected during this work, not market evidence or a
claim of economic advantage.

## Failure and repair

Start with three units bought at 100 ticks, then repeat buying one at 101 and
selling one at 100, marking at 100 with explicit zero costs. After `k` cycles,
the exact remaining acquisition pool is `303 - 3*(3/4)**k`; the average is
`101 - (3/4)**k`. Prices and quantities stay small while the denominator grows.

PR3 rendered a terminating Fraction by converting its integer coefficient with
`str`. At the buy in cycle 2,149, a reachable coefficient has 4,301 digits and
Python's unchanged 4,300-digit safety guard raises `ValueError`. The previous
4,297 fills remain committed, with inventory 3 and trade cash -2,448 ticks; the
rejected operation leaves Portfolio state unchanged. Original main completes
the bounded sequence. This is an introduced reporting-path failure, not lost
cash or evidence about fills in a real venue.

The repair obtains digits through exact `Decimal(int)` construction. It changes
neither the string guard nor caller Decimal precision, rounding, traps or flags.
Powers of two are counted by integer bits; powers of five are removed in
increasing and then decreasing exact chunks. Repeating values still divide the
original numerator by the original denominator in a local Decimal context.
Integer values have a direct exact path. Public-view invariant checks validate
finite inputs first and skip rational subtraction only when the Decimal values
are numerically equal. Changed views retain the same inventory-scaled tolerance
test. Independent review caught a negative mutable-tolerance edge case; it is
explicitly rejected before this shortcut.

The independent frozen regression uses the closed form above, not a copy of
Portfolio's recurrence. It checks all 4,609 fills in 2,304 cycles, terminal cash,
inventory, volumes, exact realized/unrealized values, timestamp, context and guard.
Additional cases cover signed 4,301-digit values, asymmetric powers of two/five,
residual other primes, integers and zero. Four separate public-API checks retain
negative-tolerance rejection. Earlier 39 accounting regressions remain unchanged.

## Protocol fixed before the resumed timing comparison

The retained [contract](evidence/exact-accounting-20261101/performance-contract.json)
pins main `1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f` as the comparison baseline.
The fixed workload is `configs/benchmark.yaml`, seed 7, with only event count
20,000, memory tracking off and plots off. There are five pairs in the order
AB, BA, AB, BA, AB. Each source/process gets one untimed full warmup and one
timed run. The timer is the scheduler loop's `wall_clock_seconds`; generation,
validation, metrics, serialization and plots are outside it. No tracing or
profiling runs during these comparisons. Limits are 90 seconds per process and
600 seconds per trial. All samples are retained.

Effective configuration and event hashes, event/scheduler counts, fill counts,
filled quantities and terminal inventory must match. Terminal net/gross P&L,
fees and rebates must agree within absolute `1e-9`. A mismatch, error or timeout
stops the run. The investigation trigger was median candidate/baseline time
at least 1.5 and every pair at least 1.2. This threshold is an engineering
decision rule, not a trading throughput requirement. Only two algorithm choices
were allowed: the numeric rendering repair and one profile-justified exact
optimization. The stopping rule forbids further timing-driven iteration here.

## Results, trials and limitations

| Source / trial | Outcome |
| --- | --- |
| Original PR3 `116a861` | Bounded growth sequence rejects cycle 2,149; no completed-work throughput ratio is calculated. |
| Numeric repair `7f767e1` | Completes the growth sequence in 60.178 s. Five full-loop paired ratios: 2.178, 2.223, 2.024, 2.427, 2.268; median **2.223**. |
| Optimized prototype `d5d2b21` | Interrupted after four timing samples when review found the mutable negative-tolerance omission. The first baseline also overlapped a growth diagnostic. All partial samples are retained and excluded from timing conclusions. |
| Reviewed optimization `7fcfb9e` | Adds the missing validation to the same optimization. Five paired ratios: 2.086, 2.310, 2.078, 2.315, 1.945; median **2.086**. |

The interruption and rerun after the validation correction are a disclosed
protocol deviation; no workload was shortened or replaced, and no timing-driven
third algorithm was selected. The reviewed run had no concurrent root diagnostic.
Its baseline elapsed times range 2.190–2.411 s; candidate times 4.638–5.102 s.
All input and behavior checks match: 20,000 market events, 80,651 scheduler
events, 780 fills, buy/sell quantities 3,712/3,808 and ending inventory -96.
The effective configuration hash is
`3f898e5cefc4c731d1f2056d8a7f9dd491b7d3ce0772af80ba0cee227468cdbc`;
the event hash is
`8a845f3b206876f8f0769d9e051d7016436355c605297f4d14fda6df2cc04237`.

A separate, uncontended growth diagnostic on reviewed source completes all
4,609 fills in **5.748 s**, with a 4,609-bit denominator and 4,611 average-cost
digits at the end. The earlier optimized prototype's overlapping 5.606 s result
is retained separately, not used as the reviewed measurement. These are single
bounded growth diagnostics, not latency distributions or a worst-case bound.

After the first trial crossed the trigger, one separate cProfile pass on the
same 20,000-event workload identified invariant rational conversions and decimal
rendering as material costs. Profiled timing is excluded from the paired samples.
The original worker's `timing_scope` text was hardcoded to say "unprofiled" even
in profile mode; the retained profile JSON explicitly has `mode: profile`, and
its timing must not be treated as an unprofiled sample.

The final implementation still crosses the investigation threshold. The modest
change in median normal-workload ratio is not evidence of a statistically
established improvement. Exact rational state and decimal output can continue
growing with partial closes; the repair removes repeated factor scanning and the
string-conversion failure, not that mathematical growth. No throughput promise,
live execution suitability or unmeasured larger-workload claim follows. The
predeclared stopping rule is reached; this review adds no further optimization.

## Reproduction and retained provenance

Install `.[dev]`, use Python 3.12 with its default integer-string guard, then run:

```bash
python tools/reproduce_exact_accounting.py
python tools/benchmark_exact_accounting.py \
  --baseline 1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f \
  --candidate 7fcfb9e071b3d4753cadb64add9f187f0b5c7c82
```

The accounting reproducer preserves the original 39-case main comparison and
separately extracts old PR3 `116a861` for the frozen 16 rendering tests. Its three
specified failures must be the actual integer-string guard exception, with the
other 13 passing; corrected source must pass all 16. It retains source/test
copies, in-process import identity, hashes, logs, JUnit and checkout metadata.
CI archives both stages. The paired benchmark archives source revisions, refuses
evidence overwrite, and saves all samples, provenance and dependency versions.
It is deliberately an explicit diagnostic rather than a CI timing gate.

The local complete gate passes 309 tests with 90.60% branch-aware package
coverage, Ruff lint/format and strict mypy on Python 3.12.14. These checks support
the implementation's correctness; their count is not an empirical trading result.

The observed measurements used the original harness preserved byte-for-byte as
`.py.txt` files in [the evidence directory](evidence/exact-accounting-20261101/),
alongside both completed reports, interrupted samples, fixed contracts, profile,
growth reports and dependencies. `manifest.json` binds their file hashes. The
cleaned archive-based benchmark tool implements the same fixed comparison; it
was added after these observations, and is not falsely identified as their
original harness. Production Portfolio source at `7fcfb9e` has SHA-256
`153cc262e8c498f416515c8251c233aedbd9cdb0c7ea5de501dd887b78f0fe35`.
Subsequent documentation/test/tool commits preserve those source bytes.

The machine used Python 3.12.14 in this saved cloud environment. Dependencies
and raw sample times are retained; timings can vary with machine load and
runtime/dependency versions. No market tape or licensed/private quant-study
inputs or results were accessed. Passive fills, queue priority, latency, fees
and adverse selection remain uncalibrated; this accounting repair resolves none
of those empirical prerequisites. Main, PR1, PR2 and their frozen evidence remain
unchanged.
