# Whole-lifecycle resource characterization

The fixed PR13 workflow established a higher process RSS high-water mark after
the scheduler returned in all four runs. The largest sampled lifecycle RSS was
in integrated reporting each time. Application-completion `ru_maxrss` was
171.52–172.30 MiB at 2,000 events and 284.57–284.69 MiB at 20,000 events.

This supplies the missing bounded whole-lifecycle characterization on the retained
cloud. It does not certify capacity, estimate untraced production cost, establish
a scaling law, authorize optimization or resolve adversarial exact-state growth.
There is no economic or calibrated execution result.

## Fixed design and provenance

[The design](END_TO_END_RESOURCE_DESIGN.md) was committed at `92cf1b7` and clarified
before measurements. The independently reviewed harness and four-entry plan were
sealed at `9416af6edb46be6f4bb15be9a5d0d3c799e30cd5`.
[plan.json](evidence/end-to-end-resources-20261117/plan.json), SHA-256
`ffcfa3bae32b4263c9e6d681b2291ffad69b0732f35e38ff62bf942792ef62b0`,
binds all effective inputs, 38 source files, design, harness and tests.

Production source is unchanged PR13
`0866bf13e449cd66e3862454eaaf424a5172c33a`. This measurement-only branch is stacked
on PR13; no production algorithm, earlier head or draft status was changed.
The November 2/4 candidates remain stopped, and PR12's indeterminate decision was
not treated as an optimization/profile/resampling gate.

Four fresh sequential workers used exposed benchmark.yaml seed-7 inputs in order
2k / 20k / 20k / 2k. Only event count, track_memory=True and write_plots=True differ
from the pinned benchmark config. Each uses the same generated list for a retained
input Parquet snapshot and the backtest; file loading was not measured. Reporting
occurs inside the original run_backtest before its locals are freed, including
lazy imports, table reload, twelve plots and summary. The cold per-worker
Matplotlib cache is separate from product outputs.

The actual retained cloud used Python 3.12.14. Static cgroup settings were a
16 GiB memory ceiling and CPU quota equivalent to four CPUs, not dedicated or
reserved resources. Worker metadata and
[observed dependencies](evidence/end-to-end-resources-20261117/checks/dependency-versions.json)
record the runtime; no packages or access/security settings were changed.

## Four observations

MiB below means 1,048,576 bytes. The predeclared primary counter is Linux
resource.getrusage(RUSAGE_SELF).ru_maxrss, converted from KiB. Equality would have
been a negative per-run result; all four were positive.

| Run | Events | HWM at scheduler return, MiB | HWM through application completion, MiB | Largest pre-completion sampled RSS, MiB | Sampled interval | Scheduler Python traced peak, MiB |
|---|---:|---:|---:|---:|---|---:|
| 001 | 2,000 | 94.398 | 172.305 | 172.773 | reporting | 4.193 |
| 002 | 20,000 | 196.691 | 284.688 | 284.820 | reporting | 40.991 |
| 003 | 20,000 | 196.156 | 284.566 | 284.727 | reporting | 40.991 |
| 004 | 2,000 | 94.570 | 171.520 | 171.984 | reporting | 4.193 |

These are individual observations and two-repeat ranges, not confidence bounds.
RSS/HWM and tracemalloc measure different scopes and times. Their difference is
not native allocation, and their ratio is not an undercount factor. VmHWM is
retained separately even where it differs from ru_maxrss; no favorable counter
was selected after results.

The external sampler nominally polls every 20 ms, with a largest observed gap of
159.95 ms. Its maxima are lower bounds and cannot locate every transient. Run
001's short engine-setup interval has no periodic sample and is correctly null,
not zero. Startup samples are retained. Post-completion bookkeeping samples are
also retained but excluded from headline sampled peaks. Cumulative HWM must not
be added across stages or interpreted as a stage allocation amount.

The reporting interval combines imports, table loading and plotting while engine
objects are live. This study does not isolate which part explains the RSS level.
It ran with tracemalloc and scalar observers; it has no uninstrumented comparator.
Worker RSS includes native-library threads, shared resident pages and allocator
retention. It excludes the controller and separately sampled child processes;
none were observed, but sampling cannot rule out transient helpers. No RSS sums
were used. The headline ends at application completion, not interpreter teardown.

## Retained products and disk scope

The table gives logical bytes from actual files, with a range only where resource
numbers change JSON text length. All eight Parquet files match byte-for-byte
within each size, as do inputs, declared nonresource documents and report summary.

| File class | 2,000 events, bytes | 20,000 events, bytes |
|---|---:|---:|
| Original synthetic input snapshot | 18,711 | 143,246 |
| Eight engine Parquets | 125,122 | 897,319 |
| Four engine JSON/YAML documents | 12,990–12,992 | 13,261–13,265 |
| Twelve PNG plots | 490,202 | 485,666 |
| Report summary | 338 | 342 |
| Task-local Matplotlib cache | 228,759 | 228,759 |

Plot bytes are larger at 2k than 20k despite the larger input at 20k. Neither
these two endpoints nor their per-event ratios establish a disk/memory scaling
law. The workloads also reach different portions of the existing regime schedule.

All final manifest entries sum to 13,117,285 logical bytes and 13,737,984
kernel-reported allocated bytes, excluding the manifest itself. This includes
source copies, samples, logs and research bookkeeping; it is not the engine-only
product footprint. The complete 292-file bundle is retained unchanged.

Original allocation observations use st_blocks*512 on this filesystem. They do
not measure cumulative writes, temporary-disk peaks, exclusive volume usage or
write amplification. Intermediate grouped stat walks are non-atomic observations
and can omit vanished temporary files. Independent per-file reconstruction is
supported for final stable totals, not earlier cache/bookkeeping/temp totals.
CI checks bytes and recomputes the original recorded allocation arithmetic; it
does not require a new checkout's allocation blocks to be equal.

## Diagnostics, trials and independent checks

All four attempts completed once, in order, with no overlap, cap hit, process
failure, replacement, warmup or resource rerun. The worker stage was 71.017 seconds;
this is trial accounting, not a throughput claim. Every worker was below 120 s,
the stage below 600 s, sampled worker RSS below 1 GiB and polled logical output
below 512 MiB. These are soft experiment/censoring limits, not SLOs or hard
containment; polling can overshoot.

Each worker log contains 29 Fontconfig “No writable cache directories” diagnostics
under this restricted cold-cache host setup. All processes returned zero,
task-local font caches exist and all 48 PNGs decode. The diagnostics are preserved
without a post-result permission/cache repair, environment change or rerun. Do not describe
the runs as warning-free or extrapolate to other cache/host conditions.

[Independent design review](evidence/end-to-end-resources-20261117/design-review.md)
preceded execution. Independent result reviewers rehashed all 291 manifest entries,
checked 152 source copies and import proofs, parsed raw counters/units and stage
timestamps, and recomputed caps and file totals. They decoded all 36 Parquets
(four inputs plus 32 engine tables) and 48 PNGs. Within-size exact parity passed;
normalization removes only the six resource fields listed in the frozen design.
Their conclusions and limits are in
[result-review.md](evidence/end-to-end-resources-20261117/result-review.md).
These are separate assistant reviews, not external human review.

Local verification passed the complete 623-test suite at 90.86% branch-inclusive
coverage, strict mypy on 37 source files, Ruff lint/format and the existing
300-event synthetic CLI smoke. The helper preflight tests and inherited PR13
integration repetitions are correctness checks, not extra resource observations.
No tests, benchmark or heavy artifact audit ran during the measured workers.
Exact-head remote CI is recorded in the draft PR and verifies saved resource data
without taking new resource samples on GitHub hardware.

## Evidence and reproduction

[The full original run](evidence/end-to-end-resources-20261117/run/manifest.json)
contains the immutable plan, archived harness/config/source, all boundary and
external RSS/disk records, worker diagnostics, original input, eight backtest
tables, twelve plots and report per attempt, plus complete audit results.
[report.json](evidence/end-to-end-resources-20261117/run/report.json) gives the
per-stage observations and parity checks. Data fingerprints include:

| Events | Exact event-stream SHA-256 |
|---:|---|
| 2,000 | d244194a0c50ab06913b4c6bdabc6e6fa70c673bb627d0f2b77609b2f7f128db |
| 20,000 | 8a845f3b206876f8f0769d9e051d7016436355c605297f4d14fda6df2cc04237 |

The complete raw copy remains at
`/workspace/lob-market-maker-resource-study/experiments/end-to-end-resources-20261117/run`.
Committed run bytes match that copy; no source-duplicate omission or partial
archive claim is needed for this study. The dependency supplement was read after
the workers from the same unchanged interpreter and is labeled accordingly.

Read-only verification takes no new resource measurements or application workers:

```bash
python -I -B tools/characterize_end_to_end_resources.py \
  --verify-evidence docs/evidence/end-to-end-resources-20261117/run
```

Compatibility checks below include application/integration engine repetitions;
they remain separate from the four resource observations:

```bash
PYTHONPATH="$PWD/src" python -m pytest --cov=lobmm --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
PYTHONPATH="$PWD/src" python -m mypy src/lobmm
```

A deliberate future reproduction must use this pinned plan/harness and a new
output path, and is a repeat of exposed inputs:

```bash
python -I -B tools/characterize_end_to_end_resources.py --run \
  --plan docs/evidence/end-to-end-resources-20261117/plan.json \
  --output experiments/lifecycle-reproduction-new-path
```

The bounded lifecycle measurement gap is now addressed at these two sizes and
this host/cache/instrumentation setup. Exact-state/public-output growth, general
accounting overhead, operational capacity and market-data/fee/latency/queue/fill
calibration remain unresolved. No optimization, profile, changed old gate,
acquisition, licensed/private E1 input, security/access change, desktop execution,
merge, deployment or trading occurred.
