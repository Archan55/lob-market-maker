# PR11 canonical ingestion cost — November 14, 2026

The primary Parquet-loading comparison has a median paired PR11/main ratio of
**1.325×**, with **100.392 ms** median paired extra time per 100,000-row batch.
It falls between the frozen engineering bands: neither the material-cost trigger
nor the low-overhead band passes. **No profile or optimization was authorized.**

The fixed secondary comparisons show larger costs for typed-event
materialization (**4.544×**) and exact decimal-text CSV (**2.939×**). Ordinary
integer CSV is **1.332×**, also between the bands. All outputs remain exact.
These are software batch measurements on one exposed synthetic stream and one
host, not market economics, venue latency, scaling or throughput guarantees.

## Question, sources and preregistration

PR11 fixed canonical fidelity but explicitly left validation cost unmeasured.
Existing book/replay benchmarks load and validate before starting their timers;
they cannot answer this ingestion question. This study closes that bounded
measurement gap without changing production source or revisiting the stopped
November 2/4 Portfolio/replay optimizations.

Timed sources are immutable main
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f` and completed PR11
`38a17f09746903938fa76b1d42268c8a75c7d3f6`. Only schema/loaders differ among
their 38 production paths. Main is an unsafe comparator on invalid inputs; it
is used only for the common valid workload and is **not a rollback choice**.
This draft is stacked on PR11 and preserves all 38 PR11 production files.

[Contract](evidence/canonical-ingestion-cost-20261114/contract.json),
[workload](evidence/canonical-ingestion-cost-20261114/workload.json) and
[design review](evidence/canonical-ingestion-cost-20261114/design-review.json)
were committed at `67455f590bfc89c0607aac7ea08e63d09e66db89` before application
imports, input generation, probes or timings. Independent helper/controller and
timer/workflow acceptance were frozen at
`6f5f7f52d96d470ba775fc5066e75da3fc762164` before the single run.

The workload is the unchanged `configs/benchmark.yaml`: seed 7, 100,000 events,
start time zero and 1 ms spacing. It was generated exactly once from archived
main. The generator/configuration and earlier synthetic periods are exposed
engineering fixtures, not new holdouts or historical market tapes. Every value
was required to be a native exact integer within the target type and below
`2**53`; failure would stop rather than select a different input.

Input fingerprint:
`926a386b9e7848f323c8d384100c8e393c6d1337916f9132d248fdd6dc28c94c`.
The three physical inputs contain the same rows: a canonical Parquet, ordinary
integer CSV, and a sensitivity CSV with `.0` on every non-null integer field.
The latter is a valid integral compatibility representation, not a claim about
typical vendor formatting. Fixed Arrow schema/codec parameters and dependency
versions are retained in the helper and evidence.

## Frozen measurement and decision rules

There are four operations and five paired observations each, **40 fresh source
processes** in total. Within each operation, source order is AB, BA, AB, BA, AB;
operation order rotates according to the committed literal schedule.

| Operation | Public call and scope |
| --- | --- |
| Parquet, primary | `load_events(canonical.parquet)`: read, normalization and typed-event decoding |
| Typed events, secondary | `events_to_frame` on prebuilt native-integer events |
| Integer CSV, secondary | `load_events(integer.csv)` |
| Decimal CSV, secondary | `load_events(decimal.csv)` compatibility sensitivity |

Each process verifies archived code/imports/inputs, performs exactly one full
untimed warmup, checks and releases its result, then collects garbage before one
measured public call. GC remains enabled and inherited thread policy is retained.
The `perf_counter_ns` wall-clock brackets only that call; CPU clocks sit outside
the wall-clock boundaries. Generation, imports, input preparation, correctness
checks, hashing, logging and audit serialization are outside the timer. Actual
file reads inside `load_events` are included. This is a **warm-cache** experiment.

All original returned values/types/schema, rows/order, binary fingerprints and
strict dictionary books must match. Returned frame schema is checked before
serialization; loads must return typed events with the original enum/native
integer field types. The retained output Parquets serialize independently
audited return values **after timing**; they are not files written by the timed
public call. Warmup/type facts are instrumented records, while physical checks
cover the stored timed values.

The primary material trigger requires median paired ratio at least 1.5,
**every** ratio at least 1.2, and median paired extra time at least 50 ms. The
low-overhead band requires median ratio at most 1.25 and every ratio at most
1.5. Otherwise classification is indeterminate **between these engineering
bands**, not a statistical test result. Secondary arithmetic is reported
separately and cannot replace the primary or authorize its profile.

Only after all 40 records pass physical/provenance checks could a crossed
primary material trigger permit one separate PR11 Parquet cProfile call.
Profile timings would be excluded from normal ratios. No production
optimization or additional candidate was allowed today.

Preparation and each worker have 120-second caps; the normal stage has a
900-second cap. Any error, timeout or source/input/output/provenance mismatch
stops with partial evidence. No retries, dropped samples, resampling, changed
sizes, extra operations or relaxed thresholds are allowed.

## Complete result

| Operation | Main median ms | PR11 median ms | Median paired ratio | Paired ratio range | Median paired extra ms | Frozen classification |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Parquet, primary | 323.725 | 412.059 | **1.324687×** | 1.171901–1.404965 | 100.392 | Indeterminate |
| Typed events | 63.520 | 316.925 | **4.543650×** | 4.527733–5.295675 | 246.929 | Secondary material cost |
| Integer CSV | 317.428 | 426.608 | **1.332489×** | 1.312522–1.406562 | 104.278 | Indeterminate |
| Decimal CSV | 325.364 | 945.866 | **2.939448×** | 2.732669–3.025203 | 633.204 | Secondary material cost |

Ratios and differences are computed **within each matched pair, then
medianed**. They are not ratios/differences of the two marginal median columns.
All raw integer nanoseconds and exact Fraction arithmetic are retained.

The five primary ratios are `1.324687, 1.404965, 1.259403, 1.401479, 1.171901`.
All show extra elapsed time on this host. The median fails the 1.5 material
condition and one pair fails 1.2; the median also exceeds the 1.25 low band.
The absolute paired delta exceeds 50 ms but cannot substitute for the other
conditions. No profile directory or profile measurement exists. The two
secondary material flags do not reopen that stopping decision.

Preparation took 8.049609569 seconds. All 40 workers and the post-run checks
completed in 515.838345017 seconds, within budget. That stage includes setup,
audits and serialization; it is **not** an operation-throughput measurement.
There was one generation and one `--run`, zero retries, omitted records,
replacements, profiles, source candidates or optimizations.

Every output has the same exact rows/schema/fingerprint/book. The 40 audit
output Parquets also have one common byte hash. Both sources keep the prepared
typed-event inputs and physical input files unchanged. All 137 protected PR11
files and all PR11 production bytes remain intact.

## Runtime uncertainty and independent review

Measurements used Python 3.12.14 in the retained saved cloud executor, with
captured dependency versions, CPU/affinity, clocks, GC, thread environment and
guard settings. Source processes do not overlap. No root/delegated tests,
profiles or artifact audits ran during normal timing. External host activity
cannot be excluded; the sampled one-minute host load ranges 0.166–1.404.
Before-call process thread counts vary by route/sample (Parquet 30, CSV 32,
typed materialization 15 or 25); no cause is inferred or sample removed.

CPU/wall ratios range 0.9965–1.0564. Whole-worker peak RSS ranges
361,908–403,132 KiB and is sampled after output auditing/serialization. It
includes imports, setup and warmup; it is not incremental operation memory or
a resource bound. Five local pairs cannot establish a population confidence
interval, a production SLO, cold-IO behavior or larger-workload scaling.

Independent reviewers checked exact arithmetic from all 40 raw records and all
20 pairs, both immutable source archives and every actual import origin,
source/config/harness/input pins, worker commands/logs, caps and ordering.
Physical review decoded **41 Parquets and two CSVs**, checking **4.3 million
row instances**, full schema/scalars, row digests, fingerprints and independent
dictionary books. The 256-file run inventory verifies. Actual import coverage
is 14 application modules per normal worker and 17 for preparation; all 38
source files are separately pinned. Recorded execution cannot logically rule
out unrecorded runs or unrelated host work.

Review roles and completed quality/publication gates are recorded in
[final review](evidence/canonical-ingestion-cost-20261114/final-review.json).
Reviewers did not rerun application operations, generation or timings. The
workflow author's independent timer/arithmetic review is disclosed alongside
the non-author reviews.

After all measurements, read-only saved-evidence verification passes. Ordinary
checks pass **252 tests / 90.62% branch-aware coverage**, with shell exit 0,
zero JUnit failures/errors/skips and 37 actual imported-module hashes checked.
Ruff lint/format (88 files), strict mypy (37 production sources) and diff checks
pass. These are compatibility checks, not additional performance samples.

## Reproduction and retained evidence

The production/source comparison is fixed to the two commits above. With the
recorded Python/dependency environment and both Git objects available:

```bash
python tools/benchmark_canonical_ingestion.py --run \
  --output experiments/canonical-ingestion-cost-independent/run
```

Use a new output directory; overwrite is refused. The original invocation was
the same command with
`--output experiments/canonical-ingestion-cost-20261114/run`. Hardware timings
will vary on another run; the complete sources/configuration, physical files,
hashes and environment distinguish input drift from runtime variation.
Use dependencies installed separately from the benchmark checkout: its strict
source inventory expects a clean tracked tree, rather than editable-install
metadata mixed beneath `src`.

The saved measurements can be verified without new application operations or
timings:

```bash
python tools/benchmark_canonical_ingestion.py --verify-evidence \
  docs/evidence/canonical-ingestion-cost-20261114/local/run
```

[Verbatim local evidence](evidence/canonical-ingestion-cost-20261114/local/),
[workspace inventory](evidence/canonical-ingestion-cost-20261114/workspace-manifest.json)
and [publication hashes](evidence/canonical-ingestion-cost-20261114/sha256-manifest.json)
retain all inputs, outputs, sources, scoped Git archives, harness snapshots,
normal records, logs and verification. Original files remain at
`/workspace/lob-market-maker-ingestion-cost/experiments/canonical-ingestion-cost-20261114/`.
The ordinary full-suite identity observer is copied verbatim as `.py.txt`.
The workspace inventory contains 267 non-bytecode files, all copied verbatim.
The complete `local/run` preserves all original relative paths, including scoped
source archives and harness copies, so the read-only verifier can audit it.

PR11's original report/evidence/provider/controller stay unchanged. Its
publication manifest remains the historical snapshot at `38a17f0`, not a live
manifest for this branch's changed workflow. The new publication manifest
records the current files. CI runs exact-head ordinary correctness/smoke,
repeats the inherited frozen canonical-fidelity verification, and verifies the
saved cost evidence/arithmetic. **CI does not rerun this ingestion-timing experiment.** Terminal
head/run/artifact verification will be recorded in the draft PR body. Remote
metadata or automated checks do not imply an independent ZIP download/audit.

### Publication verification layout correction

The first publication CI at `ce101a15d1ffd649c33cbcf03f9058b69b3f299d`
passed ordinary quality/smoke and inherited canonical correctness, but saved-cost
verification stopped at `source inventory drift`. The frozen validator scans
all files beneath `src`; editable installation can add ignored `*.egg-info`
metadata outside the application package. Exact surplus filenames were not
logged in that attempt, so this cause is inferred pending the repaired CI's
inventory diagnostic.

A controlled local check using the unchanged verifier reproduces the failure
with a diagnostic metadata marker. A separate clean archive of the exact same
HEAD verifies all saved records and arithmetic successfully. The CI-only fix
logs the installed inventory and hashes, archives the recorded HEAD, and runs
the unchanged verifier from that clean tracked tree. It does not remove metadata,
loosen a source gate, change the benchmark/helper, generate inputs or create new
timing samples. Initial failed CI and both diagnostic outcomes are retained in
the [publication repair record](evidence/canonical-ingestion-cost-20261114/publication/ci-repair-review.json).

## Practical next decision

Keep PR11's exactness guarantees. Its default Parquet path has a measured cost
between the chosen bands; typed materialization and decimal-text compatibility
deserve explicit budget awareness. Static code shows additional scalar checks
and parsing/materialization changes, but this study has no authorized profile
and does not isolate their causal shares.

Existing backtest calls on an already prepared event list do not load files;
reusing a validated list avoids repeated ingestion structurally. No combined
speedup or full-backtest timing was measured. If ingestion becomes a concrete
workload bottleneck, any optimization needs a separately justified equivalence
contract and preregistration. This study stops here.

No result resolves PR3's accounting/output-growth limits, missing authorized
market tape, reconstruction/clocks, realistic fees, queue/fill or latency
calibration. The separate Hyperliquid lead is not acquired or assumed cleared.
No economic edge, live-readiness, merge, deployment, trade, purchase,
brokerage, credential/access/security change, denied-action retry or desktop
execution is included. Main and earlier draft heads remain preserved.
