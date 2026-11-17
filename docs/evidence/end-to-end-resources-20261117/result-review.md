# Independent resource and physical-artifact review

Both reviewers examined the saved original records without application imports,
engine reruns, new resource measurements, source changes or remote operations.
They did not use the report's valid flag as their numerical evidence.

`growth_reproducer` rehashed all 291 saved entries, verified every 38-file source
copy and before/after import proof, independently parsed Linux /proc counters,
converted ru_maxrss KiB to bytes, assigned samples to half-open boundary intervals,
and recomputed strict caps and original st_blocks*512 totals. Its primary results:

| Run | Scheduler-return → application-completion ru_maxrss, bytes |
|---|---:|
| 001 | 98,983,936 → 180,674,560 |
| 002 | 206,245,888 → 298,516,480 |
| 003 | 205,684,736 → 298,389,504 |
| 004 | 99,164,160 → 179,851,264 |

All four primary comparisons are true. Largest pre-completion sampled RSS is in
reporting in each run: 181,166,080 / 298,655,744 / 298,557,440 / 180,338,688 bytes.
Post-completion sampled peaks are separately retained and excluded. Run 001 has
no periodic engine-setup sample; six zombie samples have null RSS rather than a
fabricated zero. No direct child PID was observed, without ruling out transients.

All worker elapsed times are at most 30.069 seconds. The worker stage is
71,017,082,886 ns. Largest sampled worker RSS is below 1 GiB and largest disk poll
is 4,601,654 bytes, below 512 MiB. No cap fired or worker overlapped another.

The reviewer decoded every physical input and engine Parquet (36 total) and all
48 PNGs, with exactly the twelve source-declared plot names per run. Within each
size, input/eight-Parquet byte hashes, full nonresource JSON/config documents and
report_summary bytes match. Stable final product groups match physical files.
Original block totals were recomputed, without equating them to another checkout.

`publication_integrity` independently checked all 291 manifest hashes and logical
sizes, sealed inputs/harness/design pins, 152 source copies and eight import
snapshots. It parsed 3,542 periodic samples, 32 boundaries and 343 disk polls,
confirming units, timestamps, PID/lifetime ordering, interval/tie selection and
strict binary cap arithmetic. All primary HWM outcomes and reporting sampled
maxima agree. Original final records total 13,117,285 logical bytes and 13,737,984
allocated bytes, excluding the manifest. The full 292-file bundle is retained.

The provenance audit found 29 Fontconfig “No writable cache directories” messages
per worker. Logs are identical 1,392-byte diagnostics and are preserved unchanged.
Processes returned zero, local font caches exist and all PNGs decode. No repair
or measurement rerun was requested. Sampling's largest actual gap is 159.95 ms;
early grouped stat walks remain non-atomic observations. These limits must stay
visible in the report and CI retention statements.

Verdict: GO for the bounded descriptive characterization. RSS/HWM and scheduler
tracemalloc are different quantities; subtracting them does not identify native
memory. Reporting includes lazy imports/table loads/twelve plots, without isolating
individual allocation causes. No capacity, scaling, algorithm speed, economic,
fill-calibration or exact-state-growth conclusion follows.

Publication-copy/source consistency and terminal exact-head remote CI are checked
separately before final completion. These are independent assistant reviews, not
external human review.
