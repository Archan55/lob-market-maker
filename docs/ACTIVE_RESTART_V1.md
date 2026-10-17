# Conservative active strategy restart boundary v1

A restarted client can recover its delivered evidence without recovering an
active strategy. This study closes that distinction with an **opt-in permanent
quarantine**, backed by a write-ahead synthetic command journal. It does not
resume quoting, resend a possibly sent order, recover cancellation work or
flatten exposure. Ordinary backtests retain their existing behavior.

The source baseline is PR2 `ad108ebaad2edca9b2997dab95800080d5fb6802`, stacked
on unchanged draft PR1 `d4104b70660976c6d59fe8915a75ac5616bdecd3`; main remains
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`. The [contract, safety invariants,
hypotheses and finite matrix](ACTIVE_RESTART_CONTRACT_V1.md) were saved before
experiments in `79e8ef272625edafc37dac2eb1557aa93b309a94`. Prior reports are
preserved. All orders, accounts, delays and losses remain synthetic and
uncalibrated; no profitability conclusion follows.

## Why full active resumption is refused

The existing durable inbox/checkpoint covers sent intents and delivered
execution evidence. It has no strategy client-ID counter, managed quote state,
market book/history, pending cancellation controller, scheduler/channel queue,
rate/backoff state, risk/stop controller or complete account order universe.
A delivered-evidence `complete` flag cannot supply those missing fields.

The unguarded witness constructs a fresh runtime pretending to resume the old
session. It reuses the two previous client IDs, starts with inventory zero
while the surviving venue has filled two units, and loses the pending cancel.
The simulator rejects the reused ID before any duplicate execution. This is
an unsupported naive restart witness, **not a newly discovered active-restart
bug in the old explicitly offline-only DurableClient**. It demonstrates the
reason a fresh strategy plus a recovered inventory number is insufficient.

## Implemented boundary

`lobmm.active_restart.GuardedStrategySession.create` is explicit new-session
initialization at a new journal path. A live creator can receive observations,
make strategy decisions and dispatch through a supplied synthetic sink. Every
new-order/cancel instruction is strictly validated and reserved durably before
the sink is called. New client IDs cannot repeat, even with changed payloads;
identical cancel instructions cannot repeat. Different cancel timestamps or
identifier aliases are separate instructions and are not claimed to have
unique venue effects. Strategy context, session identity and command payloads
are bound in the journal.

The versioned JSON envelope has `sha256` and `journal`; the journal contains
`version`, `session_id`, `context_sha256` and ordered `commands`. Each command
records kind, immutable payload and actual dispatch timestamp. Limits are 256
commands and 4 MiB. Persistence uses the existing same-directory temporary
write, file fsync, replacement and directory fsync path. Reservations mean
**possibly dispatched**, never accepted, filled or canceled at the venue.

`open` verifies schema, checksum, strategy context, identities, scalar types,
causality and command order, then permanently quarantines the session. It has
no rearm method. Decisions, cancel-only decisions, cached/direct dispatch and
observation mutation refuse before touching strategy state or calling a sink.
Empty or rolled-back valid journals remain quarantined. A forked copy cannot
inherit creator authority. A persistence or sink failure poisons the writer,
including interruption after a sink effect; reopening can inspect but cannot
emit. Existing files and missing/corrupt state cannot silently initialize.

`assessment(DurableClient)` is read-only. It joins every reserved new intent
exactly to the verified inbox's sent intents, rejects missing, extra or altered
orders and wrong sessions, and verifies delivered fill/report/state times do
not predate the actual reserved dispatch. Pre-dispatch absent snapshots and
unknown-order replies remain usable because they establish no execution
outcome. Legitimate delayed dispatch with later execution is accepted. The
assessment separates ledger, inventory interval, possible outstanding orders,
missing detail, cancellation reservations and whether the supplied inbox is
fully applied. It always reports `resume_allowed=false`.

## Actual process death with a surviving venue

**40 cases** cover new-order and pending-cancel dispatch, mirrored bid/ask,
at ten boundaries. The parent sends SIGKILL to a waiting client process and
checks return code -9. A fresh interpreter reopens only disk files; no cloned
runtime crosses restart. The real simulator Exchange remains alive in the
parent. In this fixture the new-order sink delivers to the venue immediately;
the cancellation sink queues transport and delivers at t50 after client death.

| Death boundary | Target reservation recovered | Target sink called |
|---|---|---|
| Before temporary creation | Absent | No |
| Partial temporary write | Absent | No |
| Complete write, before fsync | Absent | No |
| After file fsync, before replace | Absent | No |
| After replace | Present | No |
| After directory fsync | Present | No |
| Before sink | Present | No |
| Inside sink after parent effect/queue insertion | Present | Yes |
| After sink returned, before local recording | Present | Yes |
| After dispatch returned to caller | Present | Yes |

All fresh recoveries reject both decision modes plus cached new/cancel dispatch,
make zero sink calls and leave the journal unchanged. The same committed
reservation can correspond to no venue effect or a live/filled order. No retry
or exactly-once venue-effect claim is made.

The tape puts ten external units ahead of an own six-unit quote and ten behind.
A t40 trade of twelve fills **two** own units. The same t60 trade of four is
applied whether the cancel arrived or not. A t50 cancel stops the residual four;
a lost/not-sent cancel permits all **six** to fill. On the bid, true trade cash
is -200 versus -600 ticks; on the ask at104 it is +208 versus +624 ticks.
Client evidence can still be absent, with known inventory/cash zero and full
six-unit reservation. Quarantine prevents new commands; it cannot prevent
these already possible fills or liquidate their inventory.

Every case retains scenario configuration, market inputs, command timing,
committed journal, venue commands/reports/fills, death/reopen results and, where
an intent is reserved, immutable evidence files and assessment. Process death
is tested on this Linux filesystem; host power loss is not.

## Delayed evidence and account risk

Eight additional histories, mirrored bid/ask, inspect **58 delivered-evidence
prefixes** through a fresh interpreter for each assessment. They cover lost
cancel, delayed cancel, early unknown-cancel before a delayed new request, and
live residual exposure. Absence and unknown rejection retain uncertainty;
terminal quantity without detail does not invent fill economics; old acceptance
cannot resurrect exposure; late duplicate originals book no extra quantity,
cash or rebate. Six histories finish with terminal detail; two retain four
possibly executable units after a two-unit fill. Their finite tape does not
establish a terminal outcome.

Two compound histories inspect **nine more prefixes**. Their independent
hand-calculated expectations were proposed by a separate reviewer:

- Bid5@100 and ask4@104 with no reports retain inventory interval [-4,5] and nine
  reserved units. The venue fills bid2 while the client is down. A terminal bid
  cancellation without fill detail narrows the interval to [-2,2], with known
  ledger zero, two missing execution units and ask reservation four. The late
  bid detail books inventory +2/cash -200; the ask4 completion yields inventory
  -2/cash +216 and complete saved-order evidence.
- Bid5@100 and ask5@104 both complete: inventory zero, trade cash +20 ticks and
  complete, drained saved-order evidence still cannot rearm the session. This
  arithmetic is an accounting witness, not a profitability estimate.

These ten histories are constructed parent-process venue scenarios with fresh
assessment subprocesses; they are **not additional SIGKILL cases**. Their
quantity/cash/interval expectations use integer scalars, independent of the
production gate and client knowledge calculations. The Exchange supplies
synthetic reports, not an independent exchange implementation. Fees/rebates
are checked separately; trade cash is integer tick notional, not currency P&L.

## Independent review and before/after defects

A separate agent authored `test_active_restart_independent.py` without importing
the primary study fixtures or oracle. Its seven-unit 2+5 fill witness covers
both sides, lost detail, unknown rejection, pending cancellation, duplicate
fills, complete flat evidence and exact fees/rebates. It also challenges cached
commands, fork authority, ambiguous sink interruption, exact intent binding,
schema/scalar/identity checks, actual count/byte limits, valid old journals and
four actual stream/fsync/replace/directory-fsync errors. Every storage failure
prevents sink invocation and poisons the writer; reopened old/new state remains
quarantined. This suite has **19 grouped tests**, not a count of crash cases.

Three concrete findings in the newly developed implementation have pinned
identical before/after tests in `tools/reproduce_active_restart_defects.py`:

1. `79e8ef2`: valid Side enum payloads failed the strict wire parser, preventing
   all valid dispatch. JSON wire normalization corrects this fail-safe
   availability defect.
2. `b8e15a1`: wrong-strategy commands could use the session authority, including
   after a recomputed checksum. Both dispatch and verified open now bind each
   new order to the configured strategy identity. Two regression tests cover
   the shared defect.
3. `c056a05`: evidence after intent send10 but before actual dispatch100 could
   be assessed as a fill/terminal execution at20. Assessment now checks all
   applied fill, non-unknown report and query state times against dispatch.
   Both invalid forms reject; valid execution at120 and absent/unknown earlier
   observations pass. Quarantine was never released by the pre-fix defect.

The helper verifies **four pinned test failures / four corrected passes** and
retains logs, JUnit outcomes and source/test digests. These are prepublication
new-path development defects. Independent final review found no blocking issue
within the stated per-journal contract, and strengthened the same-tape
post-cancel comparison without adding scenarios.

## Limits and next decision

This is complete mediation through an opt-in research API, not an account-wide
interlock or a security boundary around arbitrary Python calls. A caller can
bypass it by directly using a strategy/sink or explicitly creating another
journal. New-session creation must never be used to replace or resume an old
session: globally never-reused identities, account/order reconciliation and
external writer fencing remain unresolved. Trusted live observation semantics
are inherited from StrategyRuntime; no new full risk manager is introduced.

Assumptions are single writer, retained files, immutable durable evidence inbox,
stable identities and POSIX replacement/fsync behavior. Checksums detect
accidents, not hostile rewriting. A valid older journal cannot emit on reopen,
but may omit later reservations; there is no anti-rollback watermark or proof
of a complete outbound/account universe. Concurrent writers, disk/controller
failure, power loss and other filesystems are not proven.

The next decision-useful step is a separately specified durable outbound and
receipt capture protocol with retained account/session identity and independent
writer fencing. It must establish how to reconcile every possibly sent request
and continue cancellation work while entry remains disabled. Until that evidence
exists, retain permanent quarantine. Full strategy/scheduler resumption is a
later decision, not an interpretation of this simulator passing its tests.

## Preservation and reproduction

Two final active studies match **327 stable JSON artifacts** byte for byte and
match the published [audit](ACTIVE_RESTART_V1.json), whose SHA-256 is
`d795ad70f5af5dcaea4aa4c8b2281670adbc03be3b3e9633d6d0d4c348e80c89`.
Temporary filenames and wall-clock timings are excluded. The immediately prior
durable study matches **128 artifacts** against exact `ad108eb`, and all **33
prior documentation files** are unchanged. The six earlier studies still match
**11,708 artifacts** against their pinned `8391031` baseline. All prior defect
helpers pass, and the smoke backtest plus canonical nine-case queue audit pass
with canonical Markdown/JSON byte equality.

Install Python 3.12+ and the repository's development dependencies in this cloud
checkout. Use fresh study directories: initialization deliberately refuses
existing durable inboxes and journals.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/lobmm
.venv/bin/python -m pytest tests/integration/test_active_restart_v1.py tests/integration/test_active_restart_independent.py -q --basetemp=experiments/active-repro-tmp --active-restart-output experiments/active-repro
.venv/bin/python tools/verify_active_restart.py experiments/active-verification-fresh
.venv/bin/python tools/reproduce_active_restart_defects.py experiments/active-review-fresh
.venv/bin/python tools/verify_durable_recovery.py experiments/durable-preservation-fresh
.venv/bin/python tools/reproduce_durable_review_defects.py
.venv/bin/python tools/verify_reordering_evidence.py
.venv/bin/python tools/reproduce_reordering_defect.py
.venv/bin/python tools/verify_reconciliation_evidence.py
.venv/bin/python tools/verify_transport_evidence.py
.venv/bin/python tools/reproduce_exposure_defects.py
.venv/bin/python tools/verify_client_stop_baseline.py
.venv/bin/python -m pytest --cov=lobmm --cov-report=term-missing --basetemp=experiments/active-all-repro-tmp
.venv/bin/python -m lobmm.cli backtest --config configs/ci.yaml --run-name ci-smoke
.venv/bin/python -m lobmm.cli experiment --config configs/queue_cancellation.yaml --name queue-cancellation --latency-multipliers 0.5,1,2
.venv/bin/python -m lobmm.cli audit-queue-study --experiment experiments/queue-cancellation --config configs/queue_cancellation.yaml --output experiments/active-canonical/QUEUE_CANCELLATION.md
```

CI checks out the PR head itself, records the checkout SHA, runs all aggregate,
repeat/preservation and before/after gates, and uploads eight versioned evidence
collections. The active collection includes scenario inputs, journals, actual
death results, surviving-venue records, all 67 constructed evidence prefixes,
preservation pairs, regression logs/JUnit results, aggregate test results and
observed dependencies. The draft PR records the verified exact head and terminal
CI/artifact links; no merge, deployment or live execution is part of this work.

Final cloud validation on Python 3.12.14 passed **1,412 tests with 92.10%
branch-aware package coverage**, Ruff lint/format and strict mypy. The focused
studies pass four primary tests and 19 independently authored grouped controls.
A Python 3.12 warning about forking the already multithreaded pytest harness led
to isolating that control in a clean bounded interpreter; the final independent
suite passes without warnings. This last change concerns test isolation only.
The committed form is also checked by exact-head CI recorded in the draft PR.
