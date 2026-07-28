# Implementation plan

Checkboxes are updated only after the corresponding work and verification have
actually completed.

## Phase 0 — Specification and planning

- [x] Preserve and inspect the existing repository.
- [x] Confirm the existing read-only project `AGENTS.md` guard.
- [x] Create `SPEC.md`.
- [x] Create `PLAN.md` with milestones and acceptance criteria.
- [x] Create `docs/assumptions.md`.
- [x] Create `docs/architecture.md`.

Acceptance criteria:

- [x] Scope, no-look-ahead boundary, event semantics, queue assumptions,
  accounting signs, and deterministic ordering are explicit.

Verification checkpoint:

- [x] Review all Phase 0 files before implementation begins.

## Phase 1 — Package and data foundations

- [x] Add `pyproject.toml`, license, ignore rules, package layout, and CI-ready
  tool configuration for Python 3.12+.
- [x] Implement enums, event/value types, price conversion, and validated YAML
  configuration.
- [x] Implement canonical CSV/Parquet load/write paths and adapter interface.
- [x] Implement strict canonical stream validation.
- [x] Implement deterministic multi-level synthetic generation with regimes.
- [x] Add unit and deterministic-seed tests.

Acceptance criteria:

- [x] Editable installation succeeds.
- [x] Synthetic Parquet generation and validation succeed.
- [x] Invalid data/configuration fail early with descriptive errors.

Verification checkpoint:

- [x] Run Phase 1 tests, Ruff, formatter check, and mypy; fix failures.

## Phase 2 — Reference order book

- [x] Implement strict/lenient aggregated L2 reconstruction.
- [x] Implement snapshots/resets and derived market features.
- [x] Add examples plus property/invariant tests for depth and uncrossed state.

Acceptance criteria:

- [x] All required book operations and validation modes are tested.
- [x] Valid events never leave negative/empty stored levels.
- [x] Identical event streams produce identical book state.

Verification checkpoint:

- [x] Run Phase 1–2 tests, Ruff, formatter check, mypy, and coverage; fix
  failures.

## Phase 3 — Scheduler, latency, orders, queue, and exchange

- [x] Implement causal-wave deterministic scheduler and ordered latency
  channels.
- [x] Implement typed requests/reports and validated order-state transitions.
- [x] Implement shared FIFO Level 2 queue overlay and all cancellation models.
- [x] Implement marketable/resting execution, partial fills, cancels, expiry,
  state audit, maker/taker economics, and price-through handling.
- [x] Add same-time, no-pre-arrival-fill, multiple-own-order, and queue tests.

Acceptance criteria:

- [x] No order fills before arrival and no cancel acts before arrival.
- [x] Same-time trade/cancel/new ordering matches `SPEC.md`.
- [x] One trade budget cannot be reused across own orders.
- [x] Filled plus remaining quantity always equals original quantity.

Verification checkpoint:

- [x] Run Phase 1–3 tests, Ruff, formatter check, mypy, and coverage; fix
  failures.

## Phase 4 — Portfolio and risk

- [x] Implement integer tick-notional accounting, cash, inventory, average cost,
  realized/unrealized P&L, fees, rebates, turnover, and exposure.
- [x] Implement mark choices and per-fill accounting assertions.
- [x] Implement projected exposure, order/open limits, loss/drawdown controls,
  quote age, spread/volatility/message limits, kill switch, and session policy.
- [x] Add accounting golden-path, fee/rebate, crossing-zero, and risk tests.

Acceptance criteria:

- [x] Gross/net P&L identities reconcile after every tested fill.
- [x] Costs are booked once with unambiguous signs.
- [x] Risk-reducing behavior is allowed while prohibited risk is rejected and
  audited.

Verification checkpoint:

- [x] Run Phase 1–4 tests, Ruff, formatter check, mypy, and coverage; fix
  failures.

## Phase 5 — Strategies and strategy runtime

- [x] Implement frozen observed-state context and delayed known account.
- [x] Implement fixed-spread, inventory-aware, and microprice/order-flow
  strategies.
- [x] Implement quote lifetime/refresh/threshold/stale/post-only management.
- [x] Add strategy math, rounding, suppression, and future-independence tests.

Acceptance criteria:

- [x] Strategy code has no reference path to true exchange state.
- [x] All calculations use only delivered backward-looking inputs.
- [x] Strategies do not emit crossed passive quotes or duplicate messages.

Verification checkpoint:

- [x] Run Phase 1–5 tests, Ruff, formatter check, mypy, and coverage; fix
  failures.

## Phase 6 — Backtesting, markouts, and metrics

- [x] Wire deterministic replay with true/observed books and pending channels.
- [x] Add warm-up, time/event limits, seed, comparison, audit, and session-end
  behavior.
- [x] Implement 100 ms/1 s/5 s as-of markouts and regime metrics.
- [x] Implement activity, P&L, inventory, execution, market, attribution, and
  engineering metrics.
- [x] Write every documented structured run artifact.
- [x] Add a manually auditable end-to-end golden path.

Acceptance criteria:

- [x] Same input and seed yield identical decision and fill artifacts.
- [x] Markouts are post-run only and use correct side signs.
- [x] Three strategies complete on one identical market stream.

Verification checkpoint:

- [x] Run Phase 1–6 tests, Ruff, formatter check, mypy, coverage, and a small
  integration replay; fix failures.

## Phase 7 — Reporting, CLI, documentation, and notebook

- [x] Implement Typer generation, validation, backtest, compare, report, and
  benchmark commands with helpful errors.
- [x] Implement console/JSON/Parquet reporting and every required plot.
- [x] Add runnable conservative configs for generation and all strategies.
- [x] Add concise package-consuming demonstration notebook.
- [x] Complete README, methodology, queue model, results, limitations, resume
  guidance, and data documentation.

Acceptance criteria:

- [x] All documented CLI commands run as written.
- [x] Reports are derived from arbitrary valid run artifacts.
- [x] Documentation prominently labels synthetic results and Level 2 limits.

Verification checkpoint:

- [x] Run Phase 1–7 tests, Ruff, formatter check, mypy, coverage, all CLI demo
  commands, comparison, and reporting; fix failures.

## Phase 8 — Performance and release verification

- [x] Profile the readable reference event loop.
- [x] Record measured bottlenecks and implement only justified structural
  improvements.
- [x] Add cached top-of-book maintenance and a deterministic live-order index
  after profiling showed repeated full-map/history scans.
- [x] Retain the readable reference design; do not add a second array/Numba path
  without event-by-event equivalence tests.
- [x] Implement measured wall-time throughput and optional peak-memory report.
- [x] Add GitHub Actions installation, lint, type, coverage, and integration
  jobs.
- [x] Review every limitation, command, schema, and acceptance claim.

Acceptance criteria:

- [x] Benchmark reports measured environment, seed, dataset size, duration, and
  throughput without a hardcoded claim.
- [x] CI is internally consistent with local verification.
- [x] No fabricated profitability or performance statement appears.

Verification checkpoint:

- [x] Run the complete command matrix below and record actual outcomes.

## Phase 9 — Portfolio hardening and version 0.2

- [x] Add explicit dataset provenance, historical checksum enforcement, and
  exact canonical event-stream fingerprints.
- [x] Add fictional-fixture-tested Tardis incremental-L2/trade and LOBSTER
  message/order-book adapters with manifest-producing ingestion commands.
- [x] Add bounded rejection backoff and separate desired, suppressed,
  risk-blocked, exchange-rejected, and sent counters.
- [x] Repair synthetic spread/depth replenishment and retune the demonstration
  for inventory/risk mechanics rather than synthetic profit.
- [x] Add collision-safe latency/queue/fee/ablation experiments and a
  one-command, GitHub-readable case study.
- [x] Make comparisons verify symbol, provenance, event count, and exact stream
  hash before comparing strategy results.
- [x] Wire every retained configuration field, including validation mode,
  marking choice, near-limit threshold, and independent quote-age risk.
- [x] Remove redundant configuration switches and the duplicate
  non-liquidating session policy.
- [x] Add repeated full-loop benchmarking with warm-up, lower-tail throughput,
  stream hash, and a separate traced-memory pass.
- [x] Bind reusable validation certificates to the exact raw stream and
  required validation settings.
- [x] Cache top-of-book values and maintain a deterministic live-order index,
  with differential/invariant coverage.
- [x] Reject duplicate or non-finite experiment dimensions and validate
  case-study manifests before publishing.
- [x] Re-run installation, lint, formatting, typing, full tests, coverage,
  ingestion smoke tests, demo publication, and representative benchmarks.

Acceptance criteria:

- [x] Real-data adapters normalize vendor formats without distributing licensed
  input and preserve source/output hashes plus licensing notes.
- [x] Every run is explicitly synthetic or historical and comparisons cannot
  silently mix streams.
- [x] Continuous risk and every retained configuration option affect runtime
  behavior and have focused tests.
- [x] Generated portfolio evidence avoids synthetic-profit ranking and explains
  sensitivity results.
- [x] The final version 0.2 verification matrix passes without exclusions or
  undocumented failures.

## Final command matrix

- [x] `python -m pip install -e ".[dev]"`
- [x] `python -m ruff check .`
- [x] `python -m ruff format --check .`
- [x] `python -m mypy src/lobmm`
- [x] `python -m pytest -q`
- [x] `python -m pytest --cov=lobmm --cov-report=term-missing`
- [x] 250 tests pass with 90.64% branch-aware whole-package coverage.
- [x] Run fictional Tardis and LOBSTER ingestion CLI smoke tests.
- [x] Generate and validate the deterministic synthetic Parquet dataset.
- [x] Complete fixed-spread backtest.
- [x] Complete inventory-aware backtest.
- [x] Complete microprice backtest.
- [x] Compare all three completed runs.
- [x] Regenerate the microprice report.
- [x] Publish the one-command case study with `demo --publish-case-study`.
- [x] Verify comparison and case-study identity failures.
- [x] Run and record both book-only and repeated full-loop benchmarks.

## Final acceptance audit

- [x] Package installs successfully.
- [x] Ruff lint and format checks pass.
- [x] mypy passes.
- [x] pytest and configured core coverage pass.
- [x] Generation and validation pass.
- [x] Three strategies backtest successfully.
- [x] Run artifacts match the documented schema.
- [x] Comparison and all report outputs succeed.
- [x] Golden-path, queue, accounting, no-look-ahead, and latency tests pass.
- [x] Benchmark reports measured throughput.
- [x] README commands are verified.
- [x] CI is present and consistent.
- [x] Assumptions and limitations are complete.
- [x] No result or performance claim is fabricated.
