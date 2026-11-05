# Exact continuous risk thresholds — November 5, 2026

Main and PR3 share a confirmed continuous-risk defect: ambient Decimal rounding can cause **both a false drawdown kill and a missed inclusive max-loss kill at default precision 28**. A separate precision-3 false loss kill also reproduces. This correction is a standalone draft against main; it changes no accounting, recovery, queue or execution model.

## Frozen question and confirmed witnesses

The [initial contract](evidence/exact-risk-20261105/contract.json) was committed before numerical execution at `6bc22f1193eb14d9565758b4132c1e36b601230d`. Run accounting and compatibility scope were clarified, still before execution, at `3743ee9`. The final contract SHA256 is `16bcd80f4b714df925d14a25a0455f6e55c0488d79477214f3e090e594e12fa9`. The independently authored 113-case file was pinned at `f67841ec5628b62dfa1f372b8b7fbe5c640e57cb`, SHA256 `1467bd43ad1e9822d26c89b7a52aea5280878cf6e40f19b74e123109ac8306bc`, before either baseline ran.

Baselines are main `1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f` and draft PR3 `ceccf0c69a3afbc09b07c993b65614ab09fef838`. Their risk/config files are byte-identical. Archived-source workers verify imported module paths and hashes; the finding predates PR3.

The independent oracle converts literal Decimals to Fractions and computes `H' = max(H, P)`, then max loss iff `P <= -L`, otherwise max drawdown iff `H' - P >= D`. It uses no production risk/rendering helper or ambient Decimal arithmetic. Thresholds are inclusive and loss retains priority.

| Frozen witness | Exact result | Both original sources |
| --- | --- | --- |
| Precision 28; H=0, D=1000, L=100000, P=`-999.99999999999999999999999999` | Drawdown `1000 - 10**-26` is below D; approve | Subtraction rounds drawdown to 1000; false MAX_DRAWDOWN kill |
| Precision 28; L=`999.99999999999999999999999999`, P exactly `-L`, D=100000 | Inclusive MAX_LOSS kill | Unary negation rounds threshold to -1000; breach missed |
| Precision 3; L=1.2345, P=-1.232, D=100 | Above loss threshold by 0.0025; approve | Unary negation rounds threshold to -1.23; false MAX_LOSS kill |

Baseline event evidence above the drawdown boundary can also be rounded even when the trigger is correct. Selected failures add caller Inexact/Rounded flags. These are directly observed failures, not conclusions inferred from the aggregate count. Each baseline has **88 regression failures / 25 passes**, with all 113 nodes collected and no errors or skips. The failures include context, invalid-input and constructor-atomicity checks; they are not 88 separate threshold defects.

## Repair and equivalence scope

The independently reviewed implementation is `3766a21`; `src/lobmm/risk.py` SHA256 is `e1c629a3129fef5b3854ee5c64f50246ee40df1d28f310822cb3b1aba609d413`.

The loss threshold uses context-free `copy_negate()`. Drawdown subtraction aligns the two finite decimal coefficients at their finer exponent. The larger aligned digit count plus one carry digit bounds the exact result's precision; an additional Etiny bound preserves the input quantum. A local context uses that derived precision and the Decimal implementation's exponent range, traps rounding/nonrepresentability and restores all caller settings. This is an exact arithmetic construction with a digit bound, not an arbitrary precision increase or tolerance change. Unsupported Decimal representation is rejected before mutation.

Finite Decimal inputs preserve their original tuples, including signed zero. String/integer/float parsing keeps the existing `Decimal(str(value))` policy but isolates invalid-text signals. P&L, stored peak and positive limits are validated before mutation. The complete RiskEvent and RiskDecision are constructed before event append/kill flags, and the high watermark commits only after the observation succeeds. Deep snapshots check event lists and deques, avoiding shallow-copy aliasing.

Inclusive limits, max-loss priority, cancellation requests and first kill reason/event stickiness remain. The existing `observe_pnl` behavior that returns an approved decision after a later safe observation while its sticky kill flag remains active is deliberately preserved. `continuous_check` still cancels live orders while that flag is active, and reduce-only behavior remains.

Compatibility limits are explicit: observation now rejects nonfinite limits even after deliberate configuration-validation bypass. A preexecution static assumption in the retained contract/review said normal RiskConfig accepted positive Infinity; a direct compatibility check disproved that assumption—Pydantic already rejects it for both fields. The corrected finding is retained in `compatibility.json`; the historical contract is unchanged. Custom activation overrides now see the previous peak until successful return; custom-hook observational equivalence is not claimed. Atomicity covers ordinary manager validation, arithmetic and event/decision construction, not whole `continuous_check`, exchange, concurrency, process termination or resource/custom-hook failures. Exact in-memory RiskEvent Decimals are checked; persisted Parquet risk fields retain their existing binary64 schema and are not claimed to preserve every decimal digit.

## Validation and trial accounting

One initial triage run was made for each pinned baseline, followed by one implementation candidate. Its first focused run passes all 113 frozen cases. Two independent source reviews found no additional concrete counterexample or needed repair. The full local gate passes **363 tests / 90.64% branch-aware coverage**, Ruff lint/format and strict mypy on 37 source files, with imported source identities verified after collection.

The corpus includes 15 fixed boundary cases in five contexts (ordinary precision 28, precision 3/HALF_EVEN, precision 3/UP, rounding traps, and clamped exponent/trap/preset-flag settings), plus conversion, ordinary decimal quantum, sticky sequence, public cancellation/pretrade, invalid-state and constructor-failure witnesses. These are exact correctness boundaries, not a strategy parameter sweep.

Initial triage used harness SHA256 `cae265165f026313cb31fd0dce33f681df2cb3fd7aa60be9a1cf5c1b8b5b1f98`. A formatting check caught one long line added by root; that format-only correction was made after triage, with the original harness bytes retained. No tests, thresholds or baseline results were replaced. Integration instrumentation was reviewed before its first run to distinguish per-risk-call context preservation from pre-existing Decimal flag changes elsewhere in the legacy engine. Final reproduction and CI use the retained final harness, with both versions identified in their reports.

Final before/after and integration evidence, reproduction commands, independent audit and verified remote CI are recorded below when validation completes. Previously exposed synthetic controls remain labeled as such; no market tape, execution calibration or profitability evidence is introduced.
