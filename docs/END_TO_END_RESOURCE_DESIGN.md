# Whole-lifecycle resource characterization, 2026-11-17

Preregistered before new application imports, generation, tests or measurements.
This follows the owner's revised instruction to choose routine modest workloads
and characterize the actual retained cloud without requiring a production SLO.

## Question, novelty and hypotheses

On unchanged PR13, where do worker-process RSS high-water marks occur across a
reproducible synthetic research lifecycle, and how many output bytes exist after
input materialization, backtest persistence and integrated plotting?

Prior studies measured accounting-algorithm scheduler time, exact arithmetic/output
growth, or canonical ingestion cost. This study changes no algorithm and tests no
speedup. It measures previously uncharacterized lifecycle scope, including native
process memory, scheduler population and reporting. November 2/4 candidates stay
stopped; PR12's indeterminate gate authorizes no optimization/profile/resampling.

Primary hypothesis: at least one fixed run establishes a new cumulative process
RSS high-water mark after Scheduler.run returns. Report the per-run result,
including a negative finding if no run does. Also report which sampled interval
has the largest observed RSS and the complete stage/product-size observations.
There is no magnitude, capacity or optimization eligibility gate. The original
scheduler-only tracemalloc scalar is a scope comparator, not the same quantity
as RSS; do not subtract the two to estimate native allocations.

The primary counter is resource.getrusage(RUSAGE_SELF).ru_maxrss converted from
Linux KiB to bytes: compare application_complete strictly greater than
scheduler_return. Equality is a negative per-run result. Report VmHWM separately
if it differs; do not select whichever counter gives a favorable answer.

## Frozen source, inputs and process budget

Application source: PR13 `0866bf13e449cd66e3862454eaaf424a5172c33a`, all 38
`src/lobmm` files pinned to its existing C1 SHA-256 manifest. Production files
must remain byte-identical. Base this new measurement-only branch/draft on PR13;
do not change any prior head or draft status. No main/old algorithm comparator.

Use configs/benchmark.yaml, random seed 7, changing only synthetic.event_count
to 2,000 or 20,000; backtest.track_memory=True; output.write_plots=True. Preserve
all other validated settings. These are exposed synthetic engineering inputs,
not market data, new holdouts or an adversarial exact-state-growth workload.

Exactly four fresh sequential worker processes, in order 2k / 20k / 20k / 2k.
No warmup, parallel application work, new seed, alternative size or replacement
worker. Each starts Python with isolated source selection and no bytecode writes.
The controller and artifact auditors are separate processes and excluded from
worker RSS. Worker native-library threads are included. Record observed child
processes separately; do not sum RSS values and double-count shared pages.

Generate the event list through the public synthetic API, persist an original
synthetic input Parquet snapshot through the public writer, validate/fingerprint
the same event list, then pass it and its certificate to run_backtest. This is a
direct-generation research workflow with a retained input snapshot, not a file
loading benchmark. The temporary frame used by the normal writer is not retained
by instrumentation. No additional load/copy of all events or public snapshots.

The writer is data.loaders.write_parquet(events, path), not the helper that
regenerates synthetic events. Before/after source proof and OS raw counters are
retained; do not require ru_maxrss and VmHWM to be bit-identical because they are
separate probes/accounting observations. Process startup before the first worker
checkpoint remains in full-process HWM and external sampling; label any such
samples explicitly rather than silently discarding them.

Use the original run_backtest(generate_plots=True) lifetime. Do not call report
generation after engine locals have been freed. Each worker uses the same fixed
run name in its own fresh directory. Set only a task-local per-worker MPLCONFIGDIR
before any application import, retaining its cold font cache as a separate file
class. Do not alter thread limits, proxy/credential variables, Decimal context,
integer-string guard, garbage collection or access/security settings.

## Stage boundaries and measurements

Use thin delegating wrappers, invoking each original function once with unchanged
arguments, without per-event hooks or frame introspection. Retain scalar boundary
observations only. No production edits or cProfile.

1. Bootstrap/imports/config: worker entry through required application imports
   and validated effective config construction. Defer report/Matplotlib imports.
2. Input: synthetic generation, retained canonical Parquet snapshot, validation
   and event fingerprint; ends immediately before run_backtest invocation.
3. Engine setup and scheduler population: run_backtest entry to Scheduler.run
   entry, including its normal certificate checks, engine objects and initial
   market/timer/session scheduling. This is not just Scheduler.__init__ cost.
4. Simulation: entry/return of original Scheduler.run. Existing tracemalloc also
   covers its adjacent timer/finalization/probe work; its scalar is retained as
   instrumented scheduler-allocation evidence, not total process memory.
5. Postprocessing: Scheduler.run return to _write_outputs entry; includes final
   timer/tracemalloc handling, regime/row/markout/metric construction.
6. Persistence: original _write_outputs entry/return, including JSON/YAML and
   eight Parquet outputs.
7. Reporting/import/return: _write_outputs return to original run_backtest return,
   including lazy report import, loading persisted tables and all twelve plots.
8. Application complete: capture final boundary RSS/HWM before any artifact
   decoding, full file hashing, cross-worker comparison or audit.

At each boundary, durably record monotonic timestamp, current /proc/self/status
VmRSS, VmHWM and resource.getrusage(RUSAGE_SELF).ru_maxrss, with raw units and bytes.
On this Linux host the latter fields use KiB; record platform/page-size facts.
OS cumulative HWM is not reset and is not a per-stage allocation estimate.
The final headline HWM is through application completion, not interpreter teardown
or a process-exit peak. Full file hashing/decoding/audits run only after all four
workers finish, never concurrently with a measured worker.

The external controller samples the worker's /proc/PID/status every 20 ms, with
actual monotonic timestamps and stage assignment performed from boundary times.
Sampled per-stage peaks are lower bounds and may miss transients; fast stages may
have no periodic sample. Show boundary readings separately. Never add stage peaks
or infer allocation types/liveness from peak differences. The full process includes
imports, observer bookkeeping, native libraries and allocator retention; it is not
a calibrated production-capacity measurement. Elapsed times serve ordering/caps
and descriptive scope only, not an algorithm speed comparison.

Record logical bytes (st_size) and kernel-reported allocated blocks (st_blocks*512
where available) at product boundaries, grouped as input snapshot; engine
JSON/YAML; eight engine Parquets; twelve plot PNGs and report_summary; cache; and
instrumentation/source bookkeeping separately. These are retained file sizes,
not cumulative writes, peak temporary disk, exclusive device usage or write
amplification. Poll total attempt-directory logical bytes every 200 ms for caps.

## Correctness, audit and stopping

Require complete ordered stage boundaries, valid monotonic timestamps/counters,
four complete attempts and source/import identity proof. Per size, input Parquet,
event/config fingerprints, all eight output Parquet bytes and normalized
nonresource behavior must agree across the two repeats. Normalize only declared
engineering timings/throughput/peak-memory fields; do not suppress unexplained
differences. Require exactly twelve expected nonempty valid PNGs and the report
summary, without demanding pixel identity across different workloads. Independent
auditors decode the actual Parquets/PNGs and recompute byte totals after resource
capture; their memory must not become the application's reported peak.

Exact normalization exclusions: diagnostics.{wall_clock_seconds,
events_per_second,peak_memory_bytes} and metrics.engineering.{wall_clock_seconds,
events_per_second,peak_memory_bytes}. Compare every other field in those JSON
documents, summary.json and run_config.yaml exactly within each size. PNG names
must match report.REQUIRED_PLOT_FILENAMES, all must decode, and no pixel/byte
identity requirement is imposed on the plots.

Operational caps: 120 seconds per worker, 600 seconds for the four-worker stage,
1 GiB sampled worker RSS and 512 MiB total attempt-directory logical output.
These are chosen experiment/censoring limits, not SLOs, hard containment or proven
maximum capacity; polling can overshoot. Stop on the first cap, timeout, execution,
source/import/fingerprint/stage/behavior/artifact error. Retain complete observed
prefixes, status and exact reason; no automatic retry, smaller replacement size,
favorable sample selection or post-result tuning. Output paths must be new.

Commit the design, reviewed harness, effective inputs and source pins before the
first worker. Freeze all four attempts before results. Publish individual values
and per-size observed ranges with units, no confidence/scaling/asymptotic claim.
No new resource measurements in CI: CI should verify saved records and meaningful
monitor/audit tests plus the inherited checks. Future deliberate reproduction
uses fresh paths and is labeled as a repeat of these exposed inputs.

Independent review must approve the design before execution and separately audit
actual source, samples, stage boundaries and physical artifacts afterward. A
negative hypothesis is publishable if the complete bounded characterization is
valid. Resource/coverage results do not resolve exact-state/output growth, market
tape admission, fees/latency/queue/fill calibration, economic edge or live safety.
No private/licensed E1 material, acquisition, denied retry, purchase, credentials,
security/access change, desktop, merge, deployment or trading.
