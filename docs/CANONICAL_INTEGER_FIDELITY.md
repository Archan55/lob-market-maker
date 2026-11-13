# Canonical integer fidelity — November 13, 2026

At default settings, canonical ingestion can change source values before strict
reconstruction sees them. The frozen main comparison has **19 false admissions**
of fractional integer fields and **one mixed-event timestamp fidelity failure**.
Both primary routes turn quantity `10.5` into `10`; the resulting six-event
streams are strict-valid and their actual Parquet bytes equal the integer
control. Validation and hashes after normalization cannot recover discarded
source values.

The single measured candidate passes all **33 fixed observations**: 21 invalid
inputs reject before returning/writing canonical output, and all 12 positive
controls remain exact. Ordinary six-event CSV/Parquet bytes and the complete
normal record are unchanged. This is synthetic ingestion correctness, not a
market-making result.

## Frozen question and source

The comparator is unchanged main
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`. This branch is standalone against
main; it does not combine PR3–10 fixes. Only `data/schema.py` and
`data/loaders.py` change among the 38 pinned production paths.

[Contract](evidence/canonical-fidelity-20261113/contract.json),
[literal inputs](evidence/canonical-fidelity-20261113/fixtures.json) and
[design review](evidence/canonical-fidelity-20261113/design-review.json) were
committed at `c89b87757abc10c4befde32c724d2485b824b165` before numerical probes.
Independent review then identified missing positive coverage for promised whole
Decimal compatibility. Amendment `ebf6acf08eb25f5fca67b7ef8d98369e2fc0dda7`
added two positive controls before any outcome, producing the final immutable
33-case corpus. Independent oracle/controller and prebaseline review were
committed at `33bc189a701fb52e285081929c295d768e01b1e8` before baseline execution.

The fixed cases cover fractional timestamp, sequence, price, quantity and side
through mapping/frame boundaries; CSV/Parquet reads and writes; Decimal
fractions; invalid mixed-epoch CSV and long fractional text; and raw event
materialization. Exact positive controls cover whole float/Decimal values,
integer strings, pure-integer epoch CSV, integral decimal/exponent CSV tokens,
Int64 boundary values and RESET's null side. NaN and UInt64 overflow are negative
controls. The existing six-event stream is exposed regression evidence, not a
fresh market holdout.

Advance required **both** mapping/frame quantity witnesses to be actually
admitted as 10 with strict-valid reconstruction on main. No alternative witness
or favorable subset could replace either. Candidate advancement requires every
frozen row/error/input/context/guard/writer-absence check, unchanged normal
CSV/Parquet bytes and full observation record, unchanged other source paths,
quality gates, independent review and exact-head CI.

The local budget was one archived-main worker and one candidate worker, each
with a 120-second cap; baseline evidence must be rehashed/reused rather than
rerun. One independently justified source repair was permitted. Inputs, criteria
and stopping rules remained fixed after outcomes. Ordinary full-suite and remote
CI repetitions are separately prescribed verification.

## Observed result

| Frozen category | Main | Candidate |
| --- | --- | --- |
| 21 invalid inputs | 19 falsely admitted; NaN/range already reject | All 21 domain-reject |
| 12 positive controls | 11 exact; mixed-event timestamps changed | All 12 exact |
| Total oracle mismatches | 20 | 0 |
| Caller context/guard or input mutations | 0 | 0 |
| Normal six-event record and actual CSV/Parquet bytes | Correct baseline | Identical |

The categories describe manifestations of canonical normalization, not 20
independent defects. The main worker records one expected corpus assertion
failure plus one normal pass; candidate records two passes, without errors or
skips. Every observation was retained before assertion.

The mixed raw-event input contains original Python integers and one exactly
integral float. Its provided float is established by ratio
`1700000000000000256/1` and hex `0x1.7979cfe362a01p+60`, not by its shorter decimal
representation. DataFrame inference changes five original integer timestamps:

| Provided exact timestamp | Main output |
| --- | --- |
| 1700000000000000001 | 1700000000000000000 |
| 1700000000000000256 (whole float) | 1700000000000000256 |
| 1700000000000000513 | 1700000000000000512 |
| 1700000000000000770 | 1700000000000000768 |
| 1700000000000001027 | 1700000000000001024 |
| 1700000000000001284 | 1700000000000001280 |

The candidate preserves all six exactly. Ordinary integer-only epoch CSV and
Int64 boundary controls already match on main; no corruption of valid pure
integer CSV is demonstrated. The mixed fractional CSV and long fractional token
are deliberately invalid rejection probes, not valid vendor data.

Normal Parquet SHA-256:
`53b6254a9115379a609f6cdc84794e0f6716265d6cc18b344e4010a19189b88e`.
Normal CSV SHA-256:
`5b8ade5bfd795008a0f6a19594e38f938babf55ca04834159b6248fa9578a9af`.

## Correction and trial history

Measured source commit: `6348303f041229d20d46d5bdc66165ba03e88040`.
Exact original-scalar comparison rejects fractional/nonfinite narrowing before
returning integers; applicable frame/file bounds remain Int64 and Int8. Direct
mapping retains its existing Python-integer range and integer-string grammar.
Native Integer/Bool/Null/String frame cast paths remain; numeric float/Decimal
columns get an exactness check. Whole values remain admitted. Bool compatibility
beyond the fixed corpus is a source-review conclusion, not a new empirical case.

CSV reading preserves original text before inference. Ordinary integer tokens
use native strict casts; other admitted integral decimal/exponent tokens use
finite Decimal parsing, original-value bounds before integer allocation, then
exact equality. Frame/mapping string grammar is kept distinct from CSV grammar.
Raw event attributes are normalized before DataFrame construction. No precision
bump, tolerance, global context or integer-string guard change is used.

One premeasurement static source repair was used. Independent review found that
`event.as_dict()` itself calls `int(side)`, allowing RESET's unvalidated raw side
to be truncated before the new guard. The final source reads original event
attributes directly. This was a static finding; no new runtime case or outcome
was used. The initial schema bytes are retained as an explicitly labeled,
hash-verified reconstruction of the sole reverse textual edit. Their SHA-256
matches the original author/reviewer pin. See the source review and retained
static-review history. No repair budget remained at candidate execution.

There was one source design, one measured candidate worker, **one premeasurement
static repair and zero result-driven repairs**. No omitted numerical attempts,
timeouts, setup failures, substituted strings, relaxed gates or timing search.
The two positive Decimal controls were added before baseline, not selected from
results. Full-suite/CI repeats are verification, not hidden candidates.

| Frozen component | SHA-256 |
| --- | --- |
| Contract | `0deb4423843996239e62f09342e88bce9a09176700ab9e0056aff192c5ad1730` |
| Inputs | `bbd59beaae6feb0cac0d0cf965ace94b055b113b984d8e505a008f00bbe9a3e7` |
| Independent provider | `9c07f5cce83d632ac5d648a31ef38d5a2fba5b107f032b3d5032ecb5ac130855` |
| Controller | `615bb5f078a76a5e486439c09c647a7db5719d63045636556fe1d258d5b64453` |
| Final schema | `c4dab3177ca329791bf4fe5e3c0413babaacc49237bda6dc5cec83c8c5dc2a49` |
| Final loaders | `d516e15e3aebfbb402444687b716cfd2eb88d3052ea80bc03bfff2fe946449f6` |

## Reproduction and verification

With project development dependencies and Python 3.12, use a new output path:

```bash
python tools/reproduce_canonical_fidelity.py --stage verify \
  --candidate "$(git rev-parse HEAD)" \
  --output experiments/canonical-fidelity-independent/verify
```

The original local commands were:

```bash
python tools/reproduce_canonical_fidelity.py --stage baseline \
  --output experiments/canonical-fidelity-20261113/baseline
python tools/reproduce_canonical_fidelity.py --stage study \
  --candidate 6348303f041229d20d46d5bdc66165ba03e88040 \
  --baseline-evidence experiments/canonical-fidelity-20261113/baseline \
  --output experiments/canonical-fidelity-20261113/study-final
```

The controller isolates Git source archives, verifies every source path and
actual imported file before/after collection/execution, pins the harness and
inputs, refuses overwrites, retains failures and verifies actual files. The
lexical/Fraction/integer-ratio oracle and dictionary book do not call production
normalization/book helpers. Reviewers inspect original saved files separately.

Two independent reviewers audited both complete retained results without
rerunning production, tests, providers or conversions. They rehashed and decoded
all **82 actual Parquets** (51 main, 31 candidate, including raw inputs and
written outputs) and **13 CSVs**, checked 1,188 frozen-to-raw cells per source,
exact float ratios/hex, independent books/fingerprints, error stages and all
input/context/guard observations. All 38 source pins, three import snapshots,
archive/frozen hashes and 114 baseline/93 candidate manifest entries verify.
The normal full record is identical; the 11 other previously correct positive
Parquets also remain byte-identical. This audits retained execution records;
records cannot logically exclude unrecorded runs.

Local ordinary checks pass **252 tests / 90.62% branch-aware coverage**, Ruff
lint/format (82 files), strict mypy (37 sources) and diff checks. Python was
3.12.14 at `/workspace/lob-market-maker-accounting-fix/.venv/bin/python`.
Direct suite execution used `PYTHONPATH=$PWD/src` and a session-finish identity
observer to supersede the older editable install without importing application
code before coverage. All 37 actual module paths/hashes match; shell exit 0 and
JUnit zero failures/errors/skips were captured.

Original evidence remains at
`/workspace/lob-market-maker-canonical-fix/experiments/canonical-fidelity-20261113/`.
[Local copies](evidence/canonical-fidelity-20261113/local/),
[workspace inventory](evidence/canonical-fidelity-20261113/workspace-manifest.json),
[publication hashes](evidence/canonical-fidelity-20261113/sha256-manifest.json) and
[independent review](evidence/canonical-fidelity-20261113/final-review.json)
record exact retained evidence and review coverage. Duplicate source/frozen
copies remain locally and are represented by hashes/Git commits. The session
observer is copied verbatim with a `.py.txt` extension.
The workspace inventory covers 222 non-bytecode files; 124 verbatim curated
copies retain every actual CSV/Parquet plus raw records, logs and JUnit. The
initial schema artifact is explicitly a hash-verified reconstruction; the
verbatim copy refers to those disclosed local reconstruction bytes.

The workflow records actual HEAD, runs ordinary quality/smoke and both archived
source workers, prints all 33 case/error/row/source ledgers and retains 14-day
head-named evidence. Terminal exact-head CI details will be verified in the
draft PR body after publication. Remote metadata or automated byte checks do
not imply an independent download/audit of remote ZIP/Parquet bytes.

## Decision and limits

Retain the reviewed two-file correction as a draft against main. It fixes the
demonstrated canonical source-fidelity path; later strict reconstruction alone
cannot detect the prior changes. The correction adds scalar validation work;
no ingestion/replay throughput benchmark or performance advantage is claimed.

This is a bounded, default-settings synthetic result. It cannot recover source
digits already lost in a supplied Float64 frame; it does preserve exact raw
values before inference under this code's control. Direct typed `MarketEvent`
streams bypassing these schema boundaries, custom object/temporal dtypes,
arbitrary formats and unbounded-input/resource guarantees are outside scope.
It does not admit real data or establish rights, reconstruction/clocks, queue
priority, fills, latency, effective fees or economic advantage. Strategy
evaluation still needs those inputs. The separate Databento review is not
duplicated or assumed cleared.

Main and earlier draft heads and their evidence remain preserved. PR3's
performance/output-growth limitations and failed November 2/4 candidates are
unchanged. No merge, deployment, trading, paid service, private E1/E2 material,
new acquisition, denied-action retry, desktop or access/security change.
