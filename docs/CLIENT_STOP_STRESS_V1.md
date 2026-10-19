# Client stop, cancellation and finite observation — version 1

**Constructed synthetic mechanics evidence.** These nanosecond timestamps and
small unit budgets are reproducibility devices, not measured venue parameters.
Accounting results do not establish profitability or empirical loss tails.

This extends draft PR #2, based on unmerged PR #1 at
`d4104b70660976c6d59fe8915a75ac5616bdecd3`. The previous PR2 head is
`88e9b720de354a83cb7bf8f8537ee6b2c539a29e`. The earlier canonical cancellation,
latency and exposure reports/configurations/tapes are unchanged. Their historic
hashes describe their pinned revisions and installed dependency environments.

## Explicit policy contract

The default remains `backtest.shutdown_policy: forced_expiry`, including its
session-end venue expiry and `mark`/best-price `liquidate` accounting choices.
Default serialized configs omit the new fields to retain the old run identity.

Opt in explicitly:

```yaml
backtest:
  shutdown_policy: client_stop
  client_stop_timestamp_ns: 100
  observation_end_timestamp_ns: 1100
  session_end_policy: mark
```

Stop and horizon are mandatory, nonnegative absolute timestamps. Stop must lie
within the selected tape, and horizon must be at least stop. A horizon inside
the tape observes its prefix; processed-event counts and stream hashes describe
that prefix, while the input-validation certificate still describes the full
supplied input. Comparisons require matching shutdown policy, stop and horizon
as well as the existing stream/provenance identity checks.

This is an orderly client stop: decision generation stops, while cancellation
transport and report reception remain available. It is not a process crash,
disconnect, cancel-on-disconnect or a guaranteed flat-position policy.

| Component | Behavior |
|---|---|
| Decisions | No `runtime.decide` at or after stop; late market/report deliveries still update observations. |
| Outstanding and in-flight entries | The client tracks every sent entry until a genuine terminal report; a stop does not withdraw a travelling request. |
| Initial shutdown cancels | One client-ID cancel per unresolved sent entry. Shutdown transport bypasses the strategy message-rate gate; its sends have separate counters. |
| Early cancel | A cancel arriving before the entry receives `unknown_order`; it does not release the reservation or remove client knowledge. |
| Retry | A later delivered acceptance triggers a cancel if no shutdown cancel remains pending. No venue-truth inspection triggers that retry. |
| Covered venue cancel | At arrival, it terminates only the still-unfilled remainder; knowledge changes later when its report arrives. |
| Late fills | Accounting books each fill at exchange time; known inventory changes only at its independently delayed report. |
| Explicit TIF | In-tape scheduled expiry remains an independent venue event; stop itself never expires orders. |
| Observation end | Scheduler cutoff is inclusive, including every same-time causal wave; remaining work and quantities are retained. |
| Final accounting | Inventory remains marked and unliquidated. `client_stop` rejects `session_end_policy: liquidate`; a realistic close needs a separate, covered execution policy. |

Historical market events win ties with stop, entries and cancels. A zero-delay
stop cancel can run in the same timestamp, but cannot undo a fill caused by the
historical event. Existing risk controllers remain unchanged before stop;
after stop the shutdown controller owns cancellation work, so late risk checks
cannot silently cancel from undelivered venue knowledge.

## Tape coverage and unresolved economics

No market events are extrapolated beyond the tape. **All post-tape entry,
cancel and explicit-expiry outcomes are censored**, since unseen fills could
precede any termination. They retain their last covered live or full pending
reservation and are listed as unobserved arrivals/expiries. Reports generated
by covered venue events may still arrive beyond tape end.
`in_flight_entries` includes deferred outcomes whose scheduled arrival has
already passed; `outcome_unobserved_after_tape` distinguishes those from requests
still travelling at the observation cutoff.

The final row's timestamp is the observation/report cutoff. `end_inventory`,
cash and P&L are the covered modeled ledger, not certified actual economics
after tape coverage. `venue_observation_end_timestamp_ns` identifies that
coverage boundary. `outstanding_orders`, `in_flight_entries` and
`reachable_inventory_min/max` bound possible unresolved unit fills. Orders
remaining in the output are last-covered state, not a claim that a later cancel
failed at an actual venue. Missing future markouts stay null.

The Boolean `accounting_complete` means that no covered live or pending order
outcomes remain unresolved. It does **not** mean zero inventory or liquidation.
`client_knowledge_complete` separately means every sent entry has a terminal
acknowledgment. The historical mark source and age remain explicit; marked
inventory has no claimed liquidation proceeds or complete future cash outcome.

A paired-prefix test demonstrates why this matters. With tape ending at 1000,
stop at 900 and cancel arrival at 1100, eight units remain unresolved and the
reachable inventory is `[-4, 4]`. Extending the identical prefix with a bid
trade at 1099 reveals one own fill before cancellation: inventory becomes +1,
cash is -99 tick-notional units, and only then does covered cancellation at
1100 close the remaining orders. All three strategies reproduce this witness.

## Independent stress evidence

[JSON audit](CLIENT_STOP_STRESS_V1.json) records **70 replay cases**, **2,204
scheduler/accounting checkpoints**, **10,980 distinct reachable inventory
values checked**, and **980 deterministic artifact hashes**. The 10 initial
families plus post-tape cancel family run across all three strategies in both
directions; four additional conservative/microprice mark cases complement the
midpoint baseline. Strategy signal coefficients are neutralized to isolate
shutdown mechanics; this is not a broad strategy-performance evaluation.

The study contains 81 tests. The independent complementary suite contains 81
more, including taker arrivals after stop, partial execution/remainder
cancellation, explicit TIF before and beyond coverage, inclusive report and
cancel boundaries, exhausted message rate, initial-time stop, and late market
receipt. Config validation and three-strategy knowledge regressions add 12
tests. They cover distinct boundaries rather than repeating the implementation.

The auditor uses no engine risk projections, queue logic, book arithmetic or
portfolio accounting formulas. Hand-derived budgets start with two external
units ahead and four own units; later displayed adds join behind. An independent
aggregate-depth ledger derives marks. A sent/live/pending quantity ledger
enumerates every possible integer partial-fill combination. A rational signed
cost pool reconciles realized/unrealized value to signed exchange-time cash,
with literal Decimal fees and rebates. Round-trip cases exercise inventory
closing; opening-only cases retain zero realized gross P&L.

An independent expected-report queue derives acceptance, fill, cancel and
rejected-cancel reports from commands and hand budgets. It checks exact delivery
times and once-only fill identity, then derives known inventory and terminal
client reconciliation. Eight corruption controls reject wrong cash, early
knowledge, missing live reservations, fees, fills, late decisions, internally
consistent omitted fill reports and duplicates with new report IDs.

Every complete replay repeats, with byte-identical Parquet/config/summary
outputs within one installed dependency environment. A saved CSV/YAML replay
without observers also reproduces those outputs. Normalized timing-free public
metrics/diagnostics and snapshots are hashed; runtime throughput/memory is not
a deterministic research result. Binary hashes need not remain equal across
different dependency versions.

| Long-side witness | Covered inventory / known | Live / pending quantity | Reachable inventory |
|---|---:|---:|---:|
| Extra trade at cancel - 1 ns | 2 / 2 | 0 / 0 | `[2, 2]` |
| Extra trade at cancel | 2 / 2 | 0 / 0 | `[2, 2]` |
| Extra trade at cancel + 1 ns | 1 / 1 | 0 / 0 | `[1, 1]` |
| Full fill before cancel | 4 / 4 | 0 / 0 | `[4, 4]` |
| Early unknown cancel, later acceptance and retry | 2 / 2 | 0 / 0 | `[2, 2]` |
| Horizon 140, before cancel arrival 160 | 1 / 0 | 7 / 0 | `[-3, 4]` |
| Horizon 160, cancel arrived, reports pending | 1 / 0 | 0 / 0 | `[1, 1]` |
| Horizon 190, entries still travelling | 0 / 0 | 0 / 8 | `[-4, 4]` |
| Entry arrival 1200 beyond tape end 1000 | 0 / 0 | 0 / 8 | `[-4, 4]` |
| Cancel arrival 1300 beyond tape end 1000 | 1 / 1 | 7 / 0 | `[-3, 4]` |

The forced-expiry counterfactual stops the same tape at 100 and expires the
remainder immediately after the one-unit historical fill. The realistic replay
admits the additional fill at 160. This illustrates an omitted execution window,
not a strategy advantage or an estimate of its real-world frequency.

## Proven knowledge defect and preserved baseline

An `unknown_order` cancel rejection carried `order_status=REJECTED`, causing
the strategy to forget the travelling entry; later acceptance could not restore
its managed quote. The fix treats that rejection as nonterminal knowledge.
Three strategy regressions fail on the pinned prior PR2 source and pass now.

[Versioned correction](CLIENT_STOP_BASELINE_CORRECTION_V1.json) compares all
27 earlier exposure cases against archived prior source in the same dependency
environment: **377 of 378 deterministic artifacts are byte-identical**. The
only necessary correction is `session-pending/deterministic_diagnostics.json`:
`exchange_rejected_quote_attempts` and `rejected_quote_attempts` change from 0
to 2 because the retained quotes now receive their later session rejections.
All exposure, fills, orders, cash, marks, P&L, configs, summaries, scheduler
snapshots and other audit measurements remain equal. Historical reports/JSON
are preserved, not regenerated in place. A separate review also checked 72
artifacts across three strategies and both legacy accounting policies without
differences after excluding measured performance fields.

## Reproduce and interpret

From the repository root, using Python 3.12 and installed `.[dev]`:

```bash
python -m pytest tests/integration/test_client_stop_stress_v1.py -q \
  --client-stop-study-output experiments/client-stop-stress-v1
python -m pytest tests/integration/test_client_stop_complementary.py -q
python tools/verify_client_stop_baseline.py
python tools/reproduce_exposure_defects.py
python -m pytest --cov=lobmm --cov-report=term-missing \
  --latency-study-output experiments/latency-stress-v1 \
  --exposure-study-output experiments/exposure-session-stress-v1 \
  --client-stop-study-output experiments/client-stop-stress-v1
```

Each case saves `tape.csv`, `run_config.yaml`, all replay tables, public outputs,
timing-free outputs and `snapshots.json`. The root `audit.json` is the source for
the published JSON. `tools/verify_client_stop_baseline.py` extracts pinned source
into temporary directories without changing checkout; it saves both earlier
and corrected evidence plus before/after knowledge regression logs.

CI checks out the exact PR head, executes the full gate, pinned-source checks,
300-event smoke and canonical nine-case experiment/audit, and retains the new
study/baseline evidence for 14 days beside the preserved earlier studies. The
artifact includes the checkout SHA, Python version and observed dependencies.

Local cloud validation on Python 3.12.14 passed **654 tests / 91.51%
branch-aware package coverage**, Ruff lint/format and strict mypy. Both
pinned-source reproduction helpers, the 300-event smoke, same-stream strategy
comparison/report and canonical nine-case audit passed. The canonical JSON
reproduced byte for byte; Markdown differs only by the expected published versus
experiment JSON-link filename. The latest study repeat verifies all 980 saved
hashes and bytes, including normalized metrics/diagnostics and snapshots.

Remaining limits include Level 2 queue uncertainty, no market response to own
orders, fixed constructed latencies, no transport loss/retry timeout or process
crash, no automatic residual-inventory close, and no empirical calibration.
Post-tape economics are deliberately unresolved. Practical use requires tape
coverage through the relevant execution window and a separately validated
closing/reconciliation policy. Nothing here connects to a live venue.
