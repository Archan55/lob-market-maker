# Exact accounting and risk: component composition

The pinned PR3–11 composition passed the fixed end-to-end checks. Exact exchange
fees and portfolio P&L reached risk unchanged even though the engine had already
created Float64 artifact rows. Replacing only risk.py with main's version caused
the predicted false drawdown halt; the composed risk checks did not halt that
case. Both versions halted at the exact inclusive loss limit.

This establishes a tested integration candidate for these cases. It is not a
new accounting/risk defect, a strategy backtest, a performance improvement or
evidence of economic advantage. No existing draft was modified or merged.

## Frozen question and source provenance

The question was selected after static inspection of the independent component
reports and source. Known boundary mechanics were deliberately targeted; this is
not an unbiased defect search or a fresh holdout. The initial design was committed
at `cc421926bd66455039424c08829374a01975e7b2`, clarified before outcomes through
`19f6f4f`, and sealed with exact harness/inputs at
`122147037b772887f739ab6987b818a986f6c531` (see the authoritative
[preregistration record](evidence/component-integration-20261115/preregistration.json)
for resolved commit IDs and hashes).

[The complete design](COMPONENT_INTEGRATION_DESIGN.md) fixes the source patches,
literal events/costs, oracle, criteria, six-attempt budget and stop rules.
[Assembly provenance](evidence/component-integration-20261115/assembly.json)
records nine production patches, 15 changed paths and all copied regression/input
hashes. C1 is the PR3–11 union on main
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`; C0 has exactly the same 38 source files
except the verbatim main risk.py. PR10's PR9 dependency is applied once, and both
PR5 and PR7 changes survive in metrics.py. The production union was committed at
`f5e9f5425900ee0a9e081eb6dba24d926bd7e490` and has not been optimized or repaired.

The fixture substitutes only a fixed desired-quote policy into the existing
managed-quote state machine. Matching, channels, ledger, risk, scheduler, metrics
and artifact writing are the composed application implementations. This isolates
wiring from adaptive policy repricing. The six-event tape and one expected maker
fill are original synthetic inputs. The 5 ns cancellation delay and literal fees
are engineering constants, not measured venue execution assumptions.

## Observed results

Each source ran each case once, in the preregistered C0/C1 order. Every case
produced the expected one-unit maker bid fill at price 100, timestamp 20. All
eleven ordered risk handoffs per run matched the exact ledger: 66 handoffs total.
All C1 decisions and per-call caller-context checks matched the independent
rational oracle.

| Fixed case | C0: composed ledger, main risk | C1: full composition |
|---|---|---|
| Fee just below 1, drawdown limit 1 | False max-drawdown halt at 20 | No halt |
| Fee 1, inclusive max-loss limit 1 | Max-loss halt at 20 | Max-loss halt at 20 |
| Ordinary fee 0.01, limits 100 | No halt | No halt |

In the first case both sources delivered exactly
`-0.99999999999999999999999999999` to risk. C0's subtraction rounded the drawdown
to 1 and set the caller's Inexact/Rounded flags at the first post-fill check.
C1 preserved the exact boundary and caller context. Both saved Float64 P&L
projections are `-1.0` (bits `bff0000000000000`). Consequently these persisted
float rows alone cannot establish the exact handoff or threshold decision.

The inclusive-loss cases requested cancellation of the accepted unit ASK at 102
at time 20; it reached the exchange and was canceled at 25. The existing risk and
strategy suppression paths sent two requests: cancellation, then terminal-order
cancel rejection. This duplicate path was permitted before outcomes. C0's false
halt follows the same path. No cancellation-liveness or post-kill-fill exclusion
claim follows. The allow-new comparison is at 20, before the common session cutoff
at 40; the no-kill path can have two decisions at 20 in separate causal waves.

On the same actual output artifacts, the canonical integer event roundtrip
preserved all six ordered rows. Inventory exposure was 20/40 = 0.5 over [0,40).
Horizon 5 used the first later midpoint at 30 and observed the one filled unit;
horizon 30 was right-censored, with zero observed units and one missing unit.
No missing observation was imputed. These checks do not independently exercise
every component change; the original component regression suites supply separate
exposed compatibility checks. No new integrated plot/case-study evaluation was
performed.

[The primary result matrix](evidence/component-integration-20261115/primary/report.json)
contains every criterion and observation. Each source/case directory beside it
contains the original spec, full trace, result, observer origin proof, input
Parquet and eight actual engine Parquets. The evaluator runs after engine return;
it derives rational cash/inventory/net/high watermark from literal inputs and
does not import production accounting or risk helpers. Original Decimal values,
captured same-object checks, ordered projections and actual import paths/hashes
are retained. Object identity itself is not recoverable from serialized floats.

## Trial accounting, checks and independent review

Primary engine attempts: six; complete: six; unexpected primary failures: zero;
retries, candidate repairs, substituted scenarios and additional selected trials:
zero. C0's false decision/context mutation is the predefined negative control,
not a newly selected defect. The study was stopped after the fixed matrix.
No timing search, profile, optimization, resampling or altered gate was used.

Before the primary cases, static review corrected harness-only API/encoding and
evidence-retention issues and tightened observation completeness. No primary-case
outcomes were inspected before those changes and the final seal. The initial
design preceded source composition and compatibility execution; inherited-suite
results were known before the final seal and are explicitly labeled exposed.
The initial legacy Ruff
RUF043 style failure was resolved with one named per-file exception; the original
PR5 regression bytes remain unchanged. No functional assertion was weakened.

The composed source passed the inherited full compatibility suite: 591 tests,
90.86% branch-inclusive coverage; mypy checked 37 source files; lint/format checks
and the existing 300-event synthetic CLI smoke passed. The new single integration
test then repeated the same six exposed cases successfully. This repeat is
separate from the six primary attempts. CI repeats the same fixed test, not a new
selected study or holdout. Exact-head remote CI status and its evidence link are
reported in the draft PR; local checks are recorded separately in
[local-checks.json](evidence/component-integration-20261115/checks/local-checks.json).

[Independent design/source audit](evidence/component-integration-20261115/design-review.md)
covered source faithfulness, patch union and immutable test/input dependencies.
Separate assistant reviewers also checked the final observer/oracle code before
execution. The final numerical/physical-artifact audit is retained with this
evidence in [result-review.md](evidence/component-integration-20261115/result-review.md).
These are independent assistant reviews, not external human review.

## Reproduction and retention

Use this branch's source, existing Python 3.12 environment and dependencies:

```bash
python -I -B tools/check_component_integration.py \
  --c1-source . \
  --output experiments/component-integration-reproduce-unique
PYTHONPATH="$PWD/src" python -m pytest tests/integration/test_component_integration.py
PYTHONPATH="$PWD/src" python -m pytest --cov=lobmm --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
PYTHONPATH="$PWD/src" python -m mypy src/lobmm
PYTHONPATH="$PWD/src" python -m lobmm.cli backtest \
  --config configs/ci.yaml --run-name component-smoke-unique
```

Output destinations must be new. The controller makes isolated C0/C1 copies,
checks all 38 source hashes and actual loaded module origins, and refuses to
overwrite evidence. Worker/stage caps are 120/900 seconds with no automatic
retry. Atomic trace prefixes retain in-flight entries on abnormal termination.
The source assembly helper requires the original pinned Git objects already
available locally and never accesses the network or merges branches.

Full raw primary output remains at
`/workspace/lob-market-maker-integration/experiments/component-integration-20261115/primary`.
The committed evidence is an explicitly documented
[publication subset](evidence/component-integration-20261115/publication-subset.json),
not a complete sealed-run archive. It retains every unique primary result and
actual synthetic artifact, omitting only 76 duplicate source-copy files, whose
exact hashes and reconstruction paths/commits are listed. The original full run
manifest is unchanged. Complete local repeat output remains in the sibling
`pytest-repeat` directory; its report/manifest and orchestration logs are copied.
CI uploads its own complete synthetic reproduction with 14-day retention; this
does not establish retention of any earlier historical workflow ZIP.

## Practical limits and next decision

The independent drafts can now be assessed as this tested composition, without
changing their heads or inferring merge readiness. PR3's measured accounting
overhead and exact rational/output growth remain unresolved. PR12's indeterminate
primary cost ratio authorized no profiling/optimization/resampling; none occurred.
The stopped November 2 and November 4 candidates remain closed. Recovery PR1/2
and their accepted-order/cancellation limits are unchanged.

Economic strategy evaluation still needs an admitted, legally usable synchronized
incremental depth/trade tape with gap/reset/reconstruction evidence, venue-specific
fee/tick/lot rules, measured feed/order/cancel/report latency uncertainty, and
defensible queue/fill calibration. Untouched evaluation windows and realistic
inventory/terminal valuation, cost and no-trade baselines must be fixed before
results. Top-of-book updates and this constructed fill do not identify passive
execution. No previously documented data/calibration or performance/output-growth
blocker is resolved by this correctness result. No data was acquired, no private
or licensed E1 material used, and no access/security, live execution, deployment
or merge action occurred.
