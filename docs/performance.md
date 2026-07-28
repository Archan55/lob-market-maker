# Measured performance and profiling

Performance claims distinguish two materially different scopes.

## Benchmark methods

`python -m lobmm.cli benchmark --config <config>` measures only canonical-event
application to the reference `L2Book`. Its timer includes `cProfile` and
`tracemalloc`, so the result is a conservative profiled diagnostic rather than
an uninstrumented capacity estimate.

`python -m lobmm.cli benchmark-full --config <config> --repeats N` measures the
complete causal scheduler, exchange, queue, strategy, portfolio, and risk loop.
It validates and fingerprints the selected stream before timing, verifies the
hash-bound certificate outside each replay timer, runs an untimed warm-up, and
then reports repeated unprofiled timings. Metric construction, Parquet/JSON
serialization, and plots are outside the timed interval; benchmark runs do not
persist those artifacts. Peak Python allocation is measured in a separate
traced pass so memory instrumentation does not contaminate the timed repeats.

The full-loop result includes every repeat, median and p95 elapsed time, median
and p05 throughput, the scheduler-to-market-event ratio, environment, seed, and
the exact canonical event-stream SHA-256.

## Version 0.2 release measurement

Measured on 2026-07-27 with Python 3.12.13 on Windows 11 build 26200 and a
12th Gen Intel Core i7-1255U (12 logical processors). The input was the
deterministic synthetic benchmark stream with seed 7.

### Complete causal event loop

Command:

```bash
python -m lobmm.cli benchmark-full \
  --config configs/benchmark.yaml \
  --event-count 20000 \
  --repeats 5
```

| Field | Measured value |
|---|---:|
| Canonical market events | 20,000 |
| Timed repeats | 5 |
| Median elapsed | 4.043673 s |
| p95 elapsed | 4.791063 s |
| Median throughput | 4,946.00 market events/s |
| p05 throughput | 4,174.44 market events/s |
| Median scheduler/market ratio | 4.03255 |
| Separate traced Python peak | 42,960,240 bytes |
| Event-stream SHA-256 | `8a845f3b206876f8f0769d9e051d7016436355c605297f4d14fda6df2cc04237` |

Elapsed samples were 4.338929, 3.640939, 4.043673, 3.867786, and 4.791063
seconds. Their corresponding throughputs were 4,609.43, 5,493.09, 4,946.00,
5,170.92, and 4,174.44 market events/second.

### Reference-book replay

Command:

```bash
python -m lobmm.cli benchmark --config configs/benchmark.yaml
```

| Field | Measured value |
|---|---:|
| Canonical market events | 100,000 |
| Profiled and traced wall time | 2.260166 s |
| Profiled throughput | 44,244.54 book events/s |
| Traced Python peak | 16,828 bytes |

The book-only number is not whole-simulator throughput. Both measurements use
synthetic input and are engineering reproducibility checks, not evidence of
trading capacity or profitability on a production market-data stream.
Ambient desktop load was not controlled, so these figures are deliberately
reported as one dated observation rather than a universal capacity claim.

## Profile-guided improvements

The readable reference design remains in place, but two low-risk structural
optimizations remove avoidable repeated scans:

- `L2Book` maintains cached best bid and ask values and verifies them against
  the underlying level maps in invariant tests.
- `OrderRegistry` maintains a deterministic live-order index rather than
  filtering every historical order for each risk and self-trade check.

Before the live-order index, a 6,000-event full-loop profile performed about
3.12 million `is_fillable` checks. The post-change profile removed that scan
from the leading cumulative costs and reduced total profiled calls from about
6.90 million to 3.78 million on the same stream.

The remaining profile is dominated by scheduler dispatch, audit-row
construction, book-view creation, marking, channel scheduling, and strategy
decisions. A separate array/Numba book is not included. Any future optimized
representation must preserve the readable reference path and pass
event-by-event book, order, fill, risk, and accounting equivalence tests.

Re-run both commands on the target hardware and representative legally
obtained data before quoting a number outside this repository.
