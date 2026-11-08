# Fixed-horizon markout aggregation — November 8, 2026

The pinned-main witness changes its pooled markout proxy from **+10 to −2** when
one nine-unit fill is represented as nine otherwise identical one-unit rows.
The quantity-weighted observed amount at the same horizon remains **−2**.
This study evaluates that measurement convention in completed simulation audits.
It does not evaluate a market-making strategy or establish economic advantage.
The initial protocol was committed as
`34fdabca867a29c48dfb1664060e01d03957a4dd`, before numerical execution or
implementation. Source baseline is main
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`.

## Question and units

`compute_markouts` reports favorable side-adjusted movement from fill price to
the first true midpoint at or after each requested horizon. `markout_ticks` is
per unit in ticks; `markout_value` is per unit in model currency. The existing
horizon means are event-row statistics, and the side means pool available rows
across horizons. Those are valid descriptive means when labeled accordingly.

The former `adverse_selection_markout` instead multiplied the mean available
per-unit currency outcome across all fill/horizon rows by all filled quantity.
It has no specified single-horizon population and includes quantities whose
outcomes are unavailable. The frozen question is whether a quantity-weighted,
fixed-horizon observed statistic preserves identical unit exposure when fills
are split, while coverage makes censoring visible.

For horizon `h`, and separately for all fills, bids and asks:

```text
Q_all = sum of authoritative fill quantities in the group
Q_observed = sum of quantities with an unambiguous finite outcome at h
A_observed = sum(quantity_i * markout_value_i,h)
weighted_mean_value = A_observed / Q_observed
weighted_mean_ticks = sum(quantity_i * markout_ticks_i,h) / Q_observed
quantity_coverage = Q_observed / Q_all
```

An observation requires both finite outcome fields. No observation gives null
means and amount, with zero quantity coverage when the denominator is positive.
An empty side group has undefined coverage. Observed zero remains numeric zero.
Unobserved quantity is explicit and is never assigned an inferred price or
multiplied by the observed mean. Horizon totals must not be added together.
Different horizon coverage also prevents interpreting them as a common-sample
decay curve.

Quantities join uniquely from authoritative fills. Consistent duplicate horizon
requests count once only in the new summaries. Conflicting outcomes or identity
metadata cannot be resolved by choosing an arbitrary row. Raw markouts and the
existing first-at-or-after timing convention remain unchanged. Empty markout
rows do not identify the configured horizons, so the new table is empty.

## Preregistered controls and selection

The [contract](evidence/markout-aggregation-20261108/contract.json),
[fixed fictional audit rows](evidence/markout-aggregation-20261108/fixtures.json),
[design review](evidence/markout-aggregation-20261108/design-review.json) and
[pre-execution compatibility clarification](evidence/markout-aggregation-20261108/preexecution-clarifications.json)
define the complete study. The static source lead preceded all new outcomes.
The clarification records metadata names, preservation of the raw API's float
price representation, and duplicate/missing-field comparisons before test freeze.

The fixed controls cover unequal quantities, identical-fill splitting, side
symmetry, mixed sides, horizon composition, duplicate horizon requests, late
censoring, different per-horizon coverage, missing rows, finite eligibility,
valid zero, all missing, empty input and unambiguous joins. They are pure
post-run audit-row examples, not a claim that the fictional fills were produced
by a realistic market. The ordinary CI300 wiring pair separately checks a full
backtest with unchanged input and configuration.

There is one aggregation candidate, at most one independently reviewed
correctness repair, and no parameter or timing search. Two already exposed Nov5
cohorts are reanalyzed read-only; no new 20k engine run is permitted. Their effect
sizes and signs are not publication gates. The stopped November 2 and November 4
performance candidates remain closed.

## Output compatibility

The new `execution_quality.markout_summary_by_horizon_ns` contains separate
`all`, `bid` and `ask` records. Each reports authoritative total quantity/events,
observed and missing quantity/events, quantity coverage, quantity-weighted tick
and per-unit currency means, and observed aggregate currency value.

The old event means retain their exact definitions and values. The old scalar
becomes explicitly unavailable under `attribution.adverse_selection_markout`;
its exact former calculation is retained as `legacy_pooled_markout_proxy` for
comparison and migration. Adjacent notes explain both conventions. Existing
ledger attribution components and residual algebra remain unchanged. Consumers
must handle the old key's new nullability and choose an explicit horizon rather
than silently substitute a different scalar.

Observed gross forward markouts include entry spread. They do not isolate
causal adverse drift, include costs, or equal terminal/realized PnL. This change
does not add fill-time causal midpoint fields or resolve the end-of-timestamp
attribution convention studied on November 7.

## Results and reproduction

The baseline executed once after the independent oracle, controller and source
review were committed as `6dc08ba1592a0c4c4cdd56bb75aa146a1a5fa9a5`. All eleven
fixed fixture probes and the one CI300 backtest completed on pinned main before
candidate implementation. The frozen new-contract tests had 43 failures, no
passes, errors or skips. Those failures include unavailable new output fields
and missing join checks; they are not 43 independent numerical defects.

| Fixed audit representation | Legacy pooled proxy | Fixed-horizon observed currency amount | Quantity support |
| --- | ---: | --- | --- |
| Two fills, quantities 1 and 9, horizon 10 | +10 | −2 | 10/10 |
| Same exposure, second fill split into nine identical unit rows | −2 | −2 | 10/10 |
| Original fills, horizons 10 and 30 | 6.25 | −2 at 10; 8.5 at 30 | 10/10 each |
| Same horizons/exposure with the fill split | 3.25 | −2 at 10; 8.5 at 30 | 10/10 each |
| Original fills, duplicate request for horizon 30 | 5 | −2 at 10; 8.5 at 30 | 10/10 each |
| Add 100 late units with no future observations | 68.75 | −2 at 10; 8.5 at 30 | 10/110 each |

The fields compare the same supplied per-unit outcomes. They do not compare
strategies or actual trading outcomes. Event counts and event-row means can
legitimately change under splitting; invariance is required only for the
quantity estimands when side, time, price, outcomes and missingness are preserved.
Splitting real orders could change execution or costs, which this experiment
does not model.

The separate horizon-censoring control has total quantity 14: coverage is 14/14
at horizon 10 but 10/14 at horizon 30. Its observed amounts are 2 and 8.5.
The mixed-side control produces a genuinely observed zero at horizon 10;
all-missing input instead has zero coverage and an unavailable amount. The
former proxy reports zero for all-missing input, which does not establish an
observed zero outcome.

The repaired candidate matches every independent quantity-summary field in all
eleven fixed probes. Its unchanged 43-case corpus passes with no errors/skips.
The CI300 pair preserves all eight actual Parquet table bytes, effective input
and configuration fingerprints, summary and nonclock diagnostics. After
restoring the old scalar from the legacy proxy and removing only the four new
output entries, every prior metric is identical. This proves the bounded
measurement change preserves execution; it does not prove calibrated fills.

### Retained cohorts: weighting and support differ

The exact Nov5 CI300 and 20k seed-7 artifacts were reanalyzed once per source,
without changing their bytes or running a new 20k engine. They were already
inspected in previous work, including the November 6/7 studies; they are not
fresh holdouts. All 24 original files match the frozen manifest and provenance.

| Previously exposed cohort | Minimum forward horizon | Existing event-row mean, ticks | Quantity-weighted mean, ticks | Observed / total quantity | Observed currency amount |
| --- | --- | ---: | ---: | ---: | ---: |
| CI300 | 100 ms | 3.03125 | 3.03125 | 80 / 135 | 2.425 |
| CI300 | 1 s | unavailable | unavailable | 0 / 135 | unavailable |
| CI300 | 5 s | unavailable | unavailable | 0 / 135 | unavailable |
| Benchmark20k | 100 ms | 0.78269231 | 1.02652926 | 7,520 / 7,520 | 77.195 |
| Benchmark20k | 1 s | 3.03672680 | 3.46744652 | 7,480 / 7,520 | 259.365 |
| Benchmark20k | 5 s | −1.30137931 | +1.44997153 | 7,026 / 7,520 | 101.875 |

The CI300 equality is a useful negative result on weighting: each fill has the
same quantity, so its observed event mean and quantity mean agree. Coverage is
only **59.2593%** at 100 ms, and no 1 s or 5 s outcomes exist. Its former proxy
is **4.0921875**, which scales the available mean to all 135 units, rather than
reporting the observed 80-unit amount of 2.425. Missing future outcomes cannot
justify that extrapolation as an observed total.

The benchmark's 5 s means have opposite signs because they weight distinct
estimands: average fill event versus average observed unit. Neither is a changed
execution outcome, a corrected profit, or evidence of market advantage. The
benchmark's legacy pooled proxy is **66.71074090311268**; it is not replaced by
one arbitrarily chosen horizon total. Every horizon and both sides are retained
in the [complete summary](evidence/markout-aggregation-20261108/retained-summary.json).
For example, the 5 s bid/ask unit means are −17.24870167 / +19.65491573 ticks,
with support 3,466/3,712 and 3,560/3,808 units. Those differences are descriptive.

The 100 ms, 1 s and 5 s labels retain the existing **first midpoint at or after**
the target convention, not guaranteed exact elapsed times. Coverage varies by
horizon, so the rows are not an identified common-population response curve.
All ledger PnL, fees/rebates, risk and old event statistics remain unchanged.
This does not resolve selection bias in censored observations. Existing report
plots continue to show event-row markouts; the new quantity summaries are
explicitly named in `metrics.json` rather than silently replacing those plots.

### Introduced precision failure and the one repair

Before any candidate numerical execution, independent source review found that
the first implementation multiplied a float outcome by integer quantity before
`fsum`. That can lose a quantity unit before cancellation. Prototype
`d6d9e6a2b1a4a2dd3a7b718cb0ce1acac2f18e1b`, metrics SHA256
`022f60f4f2bb7edd4d7fb9aa0ef780be01f344d8e97ae90ded82b6a7b2d5ae5b`, was preserved.
The [amendment](evidence/markout-aggregation-20261108/quantity-precision-amendment.json)
and one independently authored supplementary test/controller were then frozen
at `492940c25df1ba7b039e263b4227cae40b253311`, before observing the new witness.
This selection happened after the original main baseline but before either
candidate/precision outcome; it was an introduced-code correctness challenge,
not a changed primary fixture, threshold or optimization candidate.

At one horizon, two bid quantities `2**53+1` and `2**53`, with per-unit tick
outcomes `+1,−1` and currency outcomes `+0.25,−0.25`, have exact aggregate
currency value **0.25**. The prototype produced **0** for the unsplit version
and **0.25** after splitting the larger fill into `2**53` and `1`. The one-node
regression reproduced exactly `[0.0, 0.25]` versus `[0.25, 0.25]`, with one
assertion failure and no errors/skips. These quantities fit the canonical int64
schema; the example still makes no physical-market claim.

The single allowed repair accumulates integer quantity times the **exact
rational representation of each supplied binary64 outcome**. It divides exact
totals by observed quantity before converting final means to float; aggregate
amounts likewise convert only after summation. The supplementary test now
passes, including exact expected small means, not a loose absolute tolerance
that would accept zero. Original test, fixture and controller hashes were not
changed. No second repair or further search follows.

These transient dyadic sums do not introduce the repeated prime-denominator
growth of Portfolio's average-cost state. Raw per-unit observations and final
outputs retain their existing float precision and range; this is exact weighting
of those supplied values, not unlimited exact currency arithmetic. Added
post-run arithmetic costs were not benchmarked. PR3's overhead/output-growth
issue and the stopped performance candidates remain separate and unresolved.

### Complete execution and verification ledger

1. Original main baseline: one frozen corpus execution and all eleven probes,
   followed by one CI300 full replay, before implementation.
2. Preserved prototype: one supplemental precision test after its own frozen
   amendment; expected failure as recorded above. The original 43-case corpus
   was not run on that prototype.
3. One exact-weighting repair, statically reviewed before execution.
4. One supplemental after-repair test, then one original candidate corpus/probe
   worker and one CI300 candidate replay. The original main baseline was
   rehashed and reused, not rerun.
5. Four retained read-only metric workers: two sources by two original cohorts,
   no engine or timing run.
6. Ordinary full suite: **294 passes / 90.70% branch-aware coverage** on Python
   3.12.14, strict mypy and Ruff lint/format. The after-collection import audit
   verifies actual modules came from this worktree, without preimporting them
   before coverage startup. Ordinary tests and prescribed CI repetitions are
   verification, not extra research candidates.

One separate `after` command mistakenly supplied nonexistent commit
`84588542243582c15e34c9ec507cf1783af172e1`. It stopped at `git rev-parse`, before
an archive or test existed. That failure remains in
`experiments/markout-aggregation-20261108/precision-after/report.json`;
the corrected command used verified HEAD in `precision-after-final`. It was an
invocation error, not a denied action or an additional numerical trial. No
failure or observation was discarded.

The measured repaired source commit is
`789af1a89f863051b71130b93629d9829fbef324`, `metrics.py` SHA256
`90ceb80020de292c5e85e39be07188da5feac141a19887005c9e1037988fc809`.
Only `metrics.py` differs among the seven pinned production modules. Later
documentation/evidence commits do not change this implementation. The
[independent review](evidence/markout-aggregation-20261108/final-independent-review.json)
records separate source/JUnit/provenance review and actual artifact review.
The latter decoded/rehashed all 16 CI Parquets and all 24 original retained
files, and independently reproduced every horizon/side field using integer
`as_integer_ratio`/common-power-of-two arithmetic, including float bits,
quantity/count fields, coverage and nulls. Auditors ran no engines or tests.

Run from the repository with Python 3.12 and the declared development dependencies,
using new output directories and an explicit full candidate SHA:

```sh
python tools/reproduce_markout_aggregation.py --stage baseline --output experiments/verification-main
python tools/reproduce_markout_aggregation.py --stage study --candidate <full-git-sha> --baseline-evidence experiments/verification-main --output experiments/verification-candidate
python tools/reproduce_markout_precision.py --stage before --output experiments/verification-precision-before
python tools/reproduce_markout_precision.py --stage after --candidate <full-git-sha> --output experiments/verification-precision-after
python -m pytest --cov=lobmm --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m mypy src/lobmm
```

In this saved cloud, the retained interpreter is
`/workspace/lob-market-maker-accounting-fix/.venv/bin/python`. For direct pytest
or CLI commands set `PYTHONPATH="$PWD/src"`; its editable-install metadata names
an older checkout. Both archive controllers independently enforce imported
source identity and do not rely on that editable-install path.

CI uses `--stage verify` in both controllers to replicate their two pinned
sources. It explicitly checks out and records the actual PR head, logs the
source hashes, full failure-node sets/counts, fixed-case ledger and precision
assertion, and uploads evidence named by that head for 14 days. The local
retained stage is **not run in CI**; it requires the exact original saved-cloud
artifact/provenance paths. No new 20k replay substitutes for those inputs.
The PR description records the final published SHA and verified terminal CI
separately from the local measured implementation above.

The [committed evidence manifest](evidence/markout-aggregation-20261108/sha256-manifest.json)
and [original workspace manifest](evidence/markout-aggregation-20261108/workspace-manifest.json)
identify the exact files. Full original worker evidence remains at
`/workspace/lob-market-maker-markout-review/experiments/markout-aggregation-20261108/`.
Curated [local records](evidence/markout-aggregation-20261108/local/) preserve the
controlled rows, actual CI tables, logs/JUnit and failed invocation. The
[portable retained subset](evidence/markout-aggregation-20261108/retained-inputs/)
contains byte-identical fills/markouts/config files sufficient for reproducing
the target quantity summaries; it is explicitly not a complete original replay
archive. All original retained input files remain pinned separately. Artifact
metadata/upload verification must not be described as an independent ZIP audit.

The portable target summaries can also be reproduced without the original
saved-cloud paths, using the committed subset and independent oracle:

```sh
PYTHONPATH="$PWD/src" python - <<'PY'
import json
import runpy
from pathlib import Path
import polars as pl

base = Path("docs/evidence/markout-aggregation-20261108")
summary = json.loads((base / "retained-summary.json").read_text())
oracle = runpy.run_path("tests/unit/test_markout_aggregation_independent.py")["independent_quantity_summary"]
for name, cohort in summary["cohorts"].items():
    inputs = base / "retained-inputs" / name
    fills = pl.read_parquet(inputs / "fills.parquet").to_dicts()
    markouts = pl.read_parquet(inputs / "markouts.parquet").to_dicts()
    expected = cohort["candidate_metrics"]["execution_quality"]["markout_summary_by_horizon_ns"]
    assert oracle(fills, markouts) == expected
    print(name, "quantity summary reproduced")
PY
```

The practical decision is to choose an explicit horizon, weighting and support
when interpreting execution quality, and to treat the legacy pooled proxy as a
comparison-only field. No observed aggregate should be extrapolated to missing
quantity or added across horizons. Publish the scoped reviewed correction as a
standalone draft against unchanged main; keep PR1–6 independent and unchanged.

## Remaining economic prerequisites

[Metadata-only feasibility review](evidence/markout-aggregation-20261108/data-feasibility.json)
found no new usable real L2/trade data or execution calibration in the matching
saved LOB executor. Existing inputs remain synthetic or explicitly fictional.
Actual strategy evaluation still needs legally usable multi-session tapes with
reconstruction, gap/sequence and clock provenance; dated instrument and cost
schedules; and defensible queue, priority and execution-latency calibration.
No data was acquired or imported from another executor. Accounting overhead and
output growth remain unresolved.
