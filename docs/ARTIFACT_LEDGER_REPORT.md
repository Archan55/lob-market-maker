# Independent reconciliation of retained accounting artifacts

The two preregistered cohorts have **zero accounting or projection mismatches**.
An independently authored weighted-average ledger and a separate signed-pool
reference both reproduce the saved views conditional on the emitted fills,
literal configuration and recorded midpoint marks. No production defect was
found or production code changed. This closes a bounded validation gap in the
composed drafts; it does not resolve their resource costs or establish trading
advantage.

## Frozen question, provenance and selection

[Design](ARTIFACT_LEDGER_DESIGN.md) was committed at `a29830b`, clarified before
outcomes through `f675e64`, and independently reviewed. The weighted-average
auditor and hand-derived controls were sealed at `ba71671`; the independently
authored signed-pool reference was committed earlier at `f8fe5f7`. Input plan
`ebb31cd02c97c297d9239a720eda5312c7cb45cf` preceded the first reconciliation.
[Plan SHA-256](evidence/artifact-ledger-20261118/plan.json):
`11ca9a5fb462336980b4d785d12894ad3689b8cb397ec9ea00dde3688d326437`.

The artifact baseline is PR14 `ec7b6e0bce491d508c30a44b737950cda503d597`.
All 38 production files remain byte-identical to PR13
`0866bf13e449cd66e3862454eaaf424a5172c33a`. The plan binds the original
PR14 manifest/plan, input Parquet and event fingerprints, exact six file paths
per cohort, and auditor/test/design bytes. The evidence reuses the original
001 and 002 files in [PR14's retained bundle](evidence/end-to-end-resources-20261117/run/).
It does not duplicate or acquire market data.

These seed-7 synthetic records were already exposed. The 2k cohort shares a
generator/prefix with the 20k cohort; runs 003/004 were previous repeats and were
excluded before outcomes. No new seed, size, parameter search, simulation or
resource measurement was performed for this reconciliation. Earlier PR3 small
hand tapes, PR13 fee/risk controls and PR14 same-implementation parity do not
provide this absolute, independently represented normal-workload comparison.

## Method and actual trials

The primary oracle keeps an exact acquisition average and accumulated P&L on
closed units. It independently recomputes fees/rebates from literal YAML scalars,
integer cash, volumes, turnover and exposure. Original row order and contiguous
fill prefixes align snapshots; timestamps are not sorted or bisected. All
25 required snapshot fields, four inventory fields and both fill-cost fields
are checked. Integer/string/null values compare exactly; Float64 values compare
packed bits without tolerance. Repeating rationals first use the predefined
28-significant-digit HALF_EVEN view; terminating rationals project directly.

The separate reference keeps a signed acquisition pool, derives realized P&L
from cash plus remaining pool, and uses Decimal coefficient construction or an
isolated 28-digit context for projection. It imports neither the application nor
the primary auditor. Its [frozen source](evidence/artifact-ledger-20261118/independent-review/review.py)
SHA-256 is `0ebbb02e6b34ffd0f1fc65ff24a88db4ed437f42b2183c1bad09aa69c0d035a5`.

Trial ledger:

1. Thirty-six fixed mathematical controls passed before the first reconciliation.
   Static review corrected control/schema and timing ambiguities before outcomes.
2. One primary pass consumed 001 then 002, once each; no failures, retries,
   repaired auditor, changed criterion or additional cohort. Its controller
   lifetime was 10.993596328 seconds, including validation/setup/finalization.
   Worker lifetimes were 1.020803005 and 9.944940626 seconds. These are cap records,
   not throughput estimates. The complete run contains 18 inventoried files
   (6,311,421 logical bytes) plus its manifest, within the frozen caps.
3. One independent reference pass checked the same two immutable cohorts. This is
   corroboration of the same exposed records, not independent market evidence.
4. Saved-record verification checks hashes, identities and counters only. CI
   additionally performs an explicitly labeled offline arithmetic reproduction.
   Standard regression tests and the 300-event smoke backtest are software checks,
   not extra observations for this study. No PR14 resource workers are rerun.

## Results and exercised behavior

| Original cohort | Events | Snapshot rows | Fills | Partial closes | Reversals | Projection mismatches |
|---|---:|---:|---:|---:|---:|---:|
| 001 | 2,000 | 2,024 | 26 | 11 | 1 | 0 |
| 002 | 20,000 | 20,772 | 780 | 378 | 17 | 0 |

Both oracles checked 569,900 snapshot fields, 91,184 inventory fields and
1,612 fill-cost fields: **662,696 fields**, including 434,310 Float64 bit
comparisons and 102,746 repeating-rational snapshot projections. Exact accounting
identities hold before presentation. The independent reference's whole-row
expected/observed chain hashes agree and its terminal exact ledgers agree with
the primary fill-prefix evidence. All required original rows, fills and admitted
marks were consumed.

There were 806 maker fills and no taker fills. Both cohorts exercised long and
short inventory, one flat close each, partial closes and reversals. Literal
configuration was tick `0.01` USD, maker fee `0`, maker rebate `0.0002`, taker fee
`0.0003`, proportional rate `0`. Nonzero maker rebates were exercised; nonzero
fees, proportional charges and taker execution were not. The fixed hand controls
cover their mathematics but do not add empirical execution coverage.

Observed rational numerator/denominator maxima were 26/20 bits in 001 and
154/147 bits in 002. These modest cases do not bound exact-state growth or
reopen the stopped performance candidates.

One **auditor metadata discrepancy** is preserved: 001's `result.json` retains
`completed_prefix=24` from its last periodic progress update. The final row is
row 2023 at prefix 26; `fills.jsonl` contains prefixes 1..26, and both oracles
consume all 26 fills. Final row/fill totals are correct, but the terminal
completion path does not refresh this progress field. It is not a missing financial row or
accounting mismatch. Raw results and frozen code were not edited, and no rerun
was used to cosmetically remove it. For terminal coverage use the detailed row
and fill logs and the independent result, not this stale progress value.

## Reproduction and evidence

Run from this branch root with the declared Python 3.12 dependencies:

```sh
python -I -B -m pytest tests/unit/test_artifact_ledger.py -q
python -I -B tools/reconcile_artifact_ledger.py \
  --verify-evidence docs/evidence/artifact-ledger-20261118/run
python -I -B tools/reconcile_artifact_ledger.py --run \
  --plan docs/evidence/artifact-ledger-20261118/plan.json \
  --output experiments/artifact-ledger-explicit-reproduction
python -I -B docs/evidence/artifact-ledger-20261118/independent-review/review.py \
  --root .
```

The explicit reproduction refuses an existing output directory. It is a repeat
of the fixed records with its own declared cap, not a new holdout. Do not invoke
the repair option on the passing original run.

Complete retained deliverables:

- [Original primary manifest](evidence/artifact-ledger-20261118/run/manifest.json),
  [report](evidence/artifact-ledger-20261118/run/report.json),
  [001 detailed rows](evidence/artifact-ledger-20261118/run/cohorts/001/rows.jsonl),
  [002 detailed rows](evidence/artifact-ledger-20261118/run/cohorts/002/rows.jsonl).
  The manifest inventories every original row/fill/mismatch log, frozen harness,
  controls, input plan, runtime and pre/post application-import checks.
- [Independent result](evidence/artifact-ledger-20261118/independent-review/result.json),
  [independent review](evidence/artifact-ledger-20261118/independent-review/REVIEW.md),
  [saved-record integrity verification](evidence/artifact-ledger-20261118/integrity-verification.json).
- [Local validation](evidence/artifact-ledger-20261118/local-validation.json) and
  [local source proof](evidence/artifact-ledger-20261118/local-source-proof.json).

Local checks passed: focused controls, Ruff lint/format, mypy on all 37 source
modules, the complete 659-test regression suite (90.86% application coverage),
and checks recorded in local validation. Test counts describe verification scope,
not research progress. Remote CI is pending at this local seal; the PR will carry
the verified remote commit and terminal run link.

## Interpretation and remaining requirements

Independent design, source and artifact review found the predefined comparison
valid within its admission rules. The practical result is stronger confidence in
the composed drafts' persisted accounting projections on these normal synthetic
runs. It supplies no new evidence of economic advantage or calibrated fills.

Saved Float64 views cannot identify hidden exact ledger state. Recorded marks
are admitted conditional inputs, not a proof of causal book selection or complete
market observation cadence. Original workers did not retain per-row Decimal
contexts; fresh default precision 28/HALF_EVEN remains an explicit presentation
assumption. Exact risk arguments/decisions, unexercised fee/role behavior,
production capacity, PR3 performance costs and unbounded exact state/output
growth remain unresolved. No optimization or additional workload is authorized
by this result. Main and all earlier draft heads remain unchanged.

Realistic market-making evaluation still requires an admitted, synchronized L2
and trade tape with reconstruction/disconnect evidence, venue fee/rebate rules,
and defensible latency, cancellation, queue-position and passive-fill calibration.
No such new input was established here. Review the composed drafts with this
conditional validation; obtain those prerequisites before claiming execution
quality or economic benefit.
