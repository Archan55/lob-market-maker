# Near-limit inventory duration — November 6, 2026

The former `inventory.time_near_limits_fraction` measured snapshot frequency, not elapsed exposure. On fixed paths over `[100,110)`, one nanosecond and nine nanoseconds near the band both reported 50%. Inserting an unchanged same-time snapshot changed the first result to 66.7%, despite its exposure still being 10% of elapsed time. The corrected metric integrates holding time; the former statistic remains available as `observations_near_limits_fraction`.

This is a research measurement correction. On the already exposed synthetic 20k run, time in the band is **49.7019%**, while the original snapshot fraction is **49.0468%**. No strategy behavior, fills or P&L changed. This is not an economic advantage or a calibrated market-execution result.

## Frozen question and source

The [contract](evidence/inventory-duration-20261106/contract.json) was committed before numerical results at `7ac444c59ae2f002adc420a79423ca65aa26d2d1`. Independent review approved a single diagnostics bounds channel, committed before execution at `29d6dfd87e4df6c65ca02ca738aa7894cd92a423`, preserving the public `build_metrics` signature. Final contract SHA256: `b265a1d9afbb87e28be36e321ad1164e62b05757c709a02bbf4534d433217471`.

The independently authored fixtures were pinned at `4c05d72`, SHA256 `a85759c5139734098259502e554c342acc5ebe7038df66bbd5e5e18f943fc252`, before execution. The primary criteria were exact duration agreement, invariance to redundant same-time snapshots, explicit unavailable cases, unchanged other metrics, input immutability and unchanged execution artifacts. No minimum test count, favorable effect-size gate or parameter search was used.

Baseline is main `1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`. Initial working-tree source/import hashes matched that pinned baseline; final verification uses fresh Git archives. Reviewed implementation is `6ee19a6`, unchanged through all later checks:

| Source | SHA256 |
| --- | --- |
| `src/lobmm/metrics.py` | `21cc8cc5d02f66899acc7169d6678f32c3e2f9c2037dd4724f78f7b623cd8cce` |
| `src/lobmm/backtest.py` | `f77f54ba4792992ccdbeae9c9d51288544ef254477b57a37eaa80dd668425a77` |
| `src/lobmm/case_study.py` | `4509c470708ac4a3ab61690b8a14b81228f294c0a3187b680bbbe95eaf6463c4` |

## Definition and compatibility

The denominator is the actual selected market window `[S,E)`, supplied through diagnostics `session_start_timestamp_ns` and `session_end_timestamp_ns`. It is not the configured filter range or the scheduler's later drain time. The portfolio starts flat; pre-window rows establish an as-of state, and the last original row at equal timestamps governs the following interval. Duplicate transitions have zero duration. The final in-window state carries to E; rows at or beyond E, including delayed liquidation, add no exposure time.

Timestamp differences and qualifying durations are integers until the final ratio conversion. The original band predicate, including inclusive membership, is unchanged. This study does not redesign float threshold arithmetic. All rows remain in observation means, dispersion, terminal inventory and the legacy observation fraction, including post-session rows.

Both bound values `None` mean unavailable, even if a key is present. One supplied bound without its counterpart, invalid/reversed bounds or invalid/nonmonotone timestamps in an explicitly bounded input raise `ValueError`. Both endpoints equal produce zero integer duration and an unavailable fraction. Missing/nonpositive position limits also yield an unavailable time fraction. No row-count fallback is used. New integer duration and window fields make the denominator explicit; generated case tables distinguish `n/a` from a measured `0.0%`.

Historical frozen reports are unchanged. Their former time field was an observation fraction; they must not be interpreted as newly measured time statistics. Custom callers without a duration window now receive `None` for time and can use the explicitly named observation statistic. The estimate assumes the replay's complete inventory-change history, not an independently observed venue account.

## Bounded results

The frozen independent oracle partitions time at unique boundaries and rescans original row order to obtain the as-of state, rather than copying the production interval fold. The fixed cases cover irregular spacing, same-time reversals/duplicates, delayed first snapshots, pre-window carry, endpoint/post-end states, empty paths, large integer timestamp offsets, undefined inputs and presentation.

The original source produces the substantive contrast **`[0.5, 0.5, 2/3]` instead of `[0.1, 0.9, 0.1]`**. Its 39 failures / one pass also include absent metadata and policy checks; these are not 39 distinct measurement defects. The first candidate passes all 40 frozen cases. One final archived before/after verification repeats those results with identical collected nodes and no errors/skips. No source repair round was needed.

The fixed retained reanalysis uses exactly two original November 5 main artifact sets, fingerprinted before analysis. These generated SYNTH inputs were already inspected; the old 20k row fraction was already visible. They are neither fresh market data nor untouched holdouts. No 20k engine replay was run today.

| Retained input | Inventory rows | Selected duration | Qualifying duration | Old observation fraction | Correct elapsed fraction |
| --- | ---: | ---: | ---: | ---: | ---: |
| CI300 | 315 | 239,000,000 ns | 0 ns | 0% | 0% |
| Benchmark20k | 20,772 | 19,959,000,000 ns | 9,920,000,000 ns | 49.0468% | 49.7019% |

The 20k difference is approximately **0.6551 percentage points**. Original Parquet, config, metrics, diagnostics and summary bytes are unchanged. The old source reproduces the original metrics on those bytes. The new source changes only the defined inventory time/observation/metadata fields; every other metric, including engineering fields in this same-input reanalysis, matches.

For the independent retained-data oracle only, maximal adjacent runs with identical band membership are compressed by retaining each run's **first** row. Every raw timestamp is validated first, band transitions and tie order remain, and every original row still feeds production metrics and observation statistics. The oracle path sizes are 315→1 and 20,772→158; raw row hashes and counts remain recorded. This is lossless representation of the qualifying indicator path, not observation selection.

The separate new CI300 main/candidate wiring pair has identical eight Parquet table bytes, event/config fingerprints, summary, non-time metrics and unrelated diagnostics. Candidate diagnostics publish the actual `[0,239000000)` bounds. Only declared inventory time/observation/metadata fields and bounds diagnostics differ; fresh-run clock diagnostics are excluded from comparison. Fills and terminal inventory remain 27 and -15. This proves bounded wiring equivalence, not throughput or a guarantee over all possible inputs.

## Verification, provenance and reproduction

Full local checks pass: **290 tests / 90.72% branch-aware coverage**, strict mypy on 37 source files and Ruff lint/format. An initial full Ruff pass flagged the frozen test's intentional regex as `RUF043`. A reviewed exact-file style exception preserves its preregistered bytes; no runtime assertion, corpus or numerical gate changed. The [diagnostic and resolution](evidence/inventory-duration-20261106/frozen-fixture-style.json) are retained. The initial focused run, full suite, final archive checks and CI replication are separate disclosed verification purposes.

Complete [regression](evidence/inventory-duration-20261106/regression-final-report.json), [retained reanalysis](evidence/inventory-duration-20261106/retained-final-report.json), [wiring](evidence/inventory-duration-20261106/wiring-final-report.json), import identities, dependencies and raw file manifests are committed under [the evidence directory](evidence/inventory-duration-20261106). Original November 6 logs/JUnit and generated artifacts remain in `/workspace/lob-market-maker-duration-fix/experiments/inventory-duration-20261106/`; the retained November 5 input paths are pinned in the contract and were not rewritten.

The [independent final review](evidence/inventory-duration-20261106/independent-review.json) approves publication. The artifact reviewer rehashed all 24 original input files and the 16 new wiring Parquet files, and separately integrated all raw inventory rows using integer partitions and as-of lookup, without relying on the production fold or the compressed oracle. That independent calculation confirms both exact duration numerators/denominators and the unchanged non-time metrics. A second reviewer verified regression provenance, report wording and CI scope. No reviewer reran the engine or changed files.

Use Python 3.12 with the recorded dependencies and new empty output directories. The current cloud interpreter is `/workspace/lob-market-maker-accounting-fix/.venv/bin/python`; direct pytest commands need `PYTHONPATH=/workspace/lob-market-maker-duration-fix/src`. Reproducer workers use fresh Git archives and verify actual imported modules, source hashes and frozen inputs.

```bash
python tools/reproduce_inventory_duration.py --stage regression --candidate d517b4aae2a282ac89143626fe1ae49d8587ab55 --output /tmp/duration-regression-new
python tools/reproduce_inventory_duration.py --stage retained --candidate d517b4aae2a282ac89143626fe1ae49d8587ab55 --output /tmp/duration-retained-new
python tools/reproduce_inventory_duration.py --stage wiring --candidate 21073746964abaf2b7dd893ba86fecd6aa5ac469 --output /tmp/duration-wiring-new
```

The final harness SHA256 is `741cc17e1b811b5a89f47cc7c2605fd82d105b4c4ce6b332e7c46518a2ff49bd`. Each worker has a 120-second cap and overwrite protection. No cap fired or numerical sample was replaced. Forced termination cannot guarantee durability of in-memory prefixes.

CI explicitly checks out and records the PR head, runs ordinary quality gates, repeats the frozen regression and the CI300 wiring pair, and retains its evidence. **The retained November 5 reanalysis is local-only and is not run in CI**, whose runner lacks those original saved-cloud paths. No alternate retrieval or new 20k simulation substitutes for them. Verified terminal CI and remote-head links belong in the final PR description and completion response; local results alone are not CI verification.

PR1–PR4, the stopped November 2/4 candidates and prior frozen reports remain unchanged. This correction does not modify matching, queues, fills, portfolio/risk accounting, recovery or trading actions. Exact-accounting performance limits and real execution/data calibration remain unresolved.
