# Exact strict ingress conversion — November 11, 2026

Main silently admits nonrepresentable source values at default Decimal
precision. Both strict price conversion and quantity scaling turn
`1.00000000000000000000000000001` into integer `1`. Four original fictional
CSV witnesses with off-tick prices or fractional units also receive valid
canonical book certificates. Their resulting Parquet bytes equal those of
aligned controls, so downstream validation cannot recover the discarded digits.

The single candidate replaces only the two strict helper calculations with
exact coefficient/exponent divisibility. All 240 frozen observations then match
the contract, while the two ordinary adapter outputs remain byte-identical.
This is a source-ingress correctness result, not market-making advantage.

## Frozen question and scope

The comparator is main `1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`.
The same conversion source is present in PR8 and the published PR3/4 heads;
Portfolio cost-basis and risk-threshold work did not change ingress validation.
The [contract](evidence/exact-ingress-20261111/contract.json),
[fixed inputs](evidence/exact-ingress-20261111/fixtures.json) and independent
design review were committed as `8463362e57e7a99b7d03f4f942466eba29d54b86`
before any new numerical probe, oracle evaluation, test execution, CSV conversion
or candidate. The independent test/controller and final static acceptance
record were frozen in `6a2cb1dad9803a6221fc4ba0e17b600a2512167c` before baseline.

The primary prediction is false acceptance of
`1.00000000000000000000000000001` at default Decimal precision: divided by a
unit tick or multiplied by a unit quantity multiplier, it must be rejected as
nonintegral. Public, wholly fictional Tardis CSV book/trade witnesses test whether
downstream strict canonical validation can nevertheless accept already-rounded
source information. An accepted canonical stream cannot restore digits lost
before its creation.

Six fixed contexts cover ordinary precision, three directed/nearest low-precision
settings, a higher-precision control and Inexact/Rounded traps. Eighteen strict
price cases, fourteen quantity cases and eight public CSV cases are evaluated
in each context. They include exact/nonexact decimal pairs, signed generic
prices, zero policy, float-as-string semantics, int64 target values and compact
large exponents whose exact result is small. Caller context and preexisting
flags must survive finite supported conversions and semantic rejection.
This is finite correctness coverage, not a parameter search or an economic
experiment. No market tape, strategy replay, timing benchmark or PnL gate is
included.

The one candidate replaces only strict price/quantity arithmetic with exact
coefficient/exponent integer divisibility. It must cancel exponents and test
denominator divisibility before constructing unnecessarily large powers, rather
than raise Decimal precision or relax a tolerance. The independent oracle uses
finite-decimal text/rational arithmetic and explicit compact-exponent controls.

`price_to_ticks(strict=False)` retains its literal existing Decimal branch.
`floor_price_to_ticks` and `ticks_to_price` remain unchanged. Generic signed and
zero strict prices remain permitted; adapter positivity remains a separate
rule. Float inputs retain `Decimal(str(value))` meaning, not recovery of original
decimal text from binary64. The context-preservation claim covers parseable
finite values and positive finite units; malformed/nonfinite parsing and whole
adapter-state atomicity are outside it.

## Comparators, budget and stopping rule

The main baseline runs once; its retained evidence is rehashed and reused for
the candidate. Each source has one bounded corpus worker, including two normal
existing fictional adapter conversions at default context. Those controls must
preserve actual canonical rows/schema, persisted Parquet bytes, event hashes,
diagnostics and strict validation. Their inputs were already exposed in prior
work and are not holdouts.

The budget is one candidate and at most one independently justified correctness
repair, with the same inputs and criteria. Each worker has a 120-second cap;
existing evidence is never overwritten. The prescribed ordinary full suite and
exact-head CI are separate compatibility/replication checks. Stop on an
unresolved witness, oracle, provenance, quality or budget failure. No favorable
input substitution, changed gate or numerical search is permitted.

Before publication, independent reviewers must challenge the actual source,
imports, original CSV literals, complete observation ledger, JUnit and actual
canonical output files. Full project checks, at least 90% branch-aware coverage,
Ruff and strict mypy remain required. A justified change belongs in an independent
draft against main, followed by verified remote exact-head CI.

## Boundaries and unresolved prerequisites

LOBSTER price/scale division and seconds-to-nanoseconds multiplication occur
upstream of these shared helpers and can themselves round. This bounded helper
study does not close that separate pipeline limitation. Nor can it recover
source digits already lost in a float DataFrame, identify true queue position
or establish economically advantageous market making.

No new authorized real tape or execution calibration is present. The separate
quant worker owns Nasdaq rights/resource review; this task makes no Nasdaq
request or sample acquisition. Existing real-data, rights, clock/reconstruction,
cost and execution-calibration prerequisites remain. No private/licensed E1/E2
material or blocked commit/publication is involved. Main, PR1–8 and the stopped
November 2/4 performance candidates are preserved; no merge, deployment,
trading, purchase, access/security change or desktop execution is authorized.

## Results and practical decision

The measured candidate is `238843475e757b8efec0f0a7feffd4cb511e2c10`.
Only `types.py` and `data/_adapter_utils.py` differ among the 38 pinned production
files. Later documentation/evidence changes do not alter measured source,
tests, inputs or controller.

| Fixed context | Main semantic mismatches | Main status-flag changes | Candidate, either category |
| --- | ---: | ---: | ---: |
| Default precision 28 | 10 | 14 | 0 |
| Precision 3, half-even | 18 | 23 | 0 |
| Precision 3, round up | 8 | 23 | 0 |
| Precision 3, round down | 17 | 23 | 0 |
| Precision 50 | 0 | 4 | 0 |
| Precision 28, Inexact/Rounded traps | 11 | 14 | 0 |
| Total over 240 fixed observations | 64 | 101 | 0 |

These categories overlap: 101 observations violate at least one check, not
101 separate defects. No caller precision, rounding setting, outer context or
integer-string guard changed. Main changes in-profile status flags; some
operations also admit a fractional value, change a valid integer or leak a
Decimal trap exception instead of the intended semantic error. Higher precision
avoids semantic mismatches in this fixed corpus but still changes flags; a
precision increase is not an exactness contract for arbitrary finite strings.

At default precision, each of the four off-price/off-quantity book/trade CSVs
preserves its original literal string at the bound helper, then becomes an
admitted integer stream with strict reconstruction, nonempty input and zero
validation issues. Each bad output is byte-identical to its corresponding
aligned control. The candidate rejects all four with `MarketDataAdapterError`
and produces no canonical file for them. All exact positive controls remain
admitted. The other finite strict helper results, exception classes, full
context snapshots and preexisting flags match the independent oracle/policy.

Both ordinary controls preserve actual Parquet bytes, canonical rows/schema,
event fingerprints, diagnostics, input hashes and complete strict certificates.
The Tardis control has 11 rows and the LOBSTER control 17. All eight ordinary
legacy conversion controls are unchanged. The six context test groups fail on
main while its two compatibility groups pass; all eight candidate groups pass,
with no errors or skips. Group counts are a compact execution ledger, not an
independent-defect count or the measure of research progress.

The decision is to retain this narrow strict-conversion repair as an independent
draft against main. It restores rejection before canonicalization for the
demonstrated loss path. It does not admit a real dataset, fix every adapter
calculation or alter any strategy/exchange behavior. Whole LOBSTER ingress,
market-data rights and execution/economic calibration remain separate.

## Provenance, trial history and review

| Frozen component | SHA-256 |
| --- | --- |
| Contract | `f28639e107b711ea9c289a22701a1e03275c729049d79b2afafe1f525276d71b` |
| Inputs | `dbc8f57942b24a94a9fd67816d0e9d350c12cdfe35f40e35b2e5d1c545962144` |
| Independent test/provider | `ca2d76e7336206af5dff46e935621c7ede5211757baee47dce25ebbcd168a8e7` |
| Controller | `bbb04f068e9c58d16b50df5926677b9bff423e2be65063054c227678525e69e1` |
| Candidate types | `a44d251f36eb56bac4944f2d8155c923516d5920ed9b5a36e946caa0c063235b` |
| Candidate adapter utilities | `e27af63e0ea40367922ec49eaf690d66eb83cd62fc748be297116c7347161172` |

Exactly one main worker and one candidate worker executed locally. Each ran the
same 240 observations, two normal adapter conversions and eight ordinary legacy
controls. The candidate reused and rehashed the complete baseline; it did not
rerun main. There was one candidate, zero numerical repairs, no omitted trials,
timeouts, worker infrastructure failures, changed strings/contexts or relaxed
criteria. Source typing/style adjustments occurred before candidate execution,
without numerical variants. Full-suite and CI repetitions are separately
prescribed verification, not additional selection trials.

The static source lead came from the owner conversation; it was unverified when
selected. Independent reviewers established novelty versus PR3/4, challenged
the contract and frozen harness, and reviewed the actual candidate before its
measurement. The lexical/rational oracle does not call production conversion
helpers. Compact exponent cases use explicit separate hand expectations and
avoid constructing giant uncancelled oracle powers.

Independent actual-result review rehashed and decoded all 52 produced Parquets
(32 main, 20 candidate), all 144 generated CSVs and the four original fixture
inputs. It checked the literal helper-call prefixes, independent canonical
schema/book accounting and fingerprints, strict certificates, all raw context
snapshots and the complete observation ledger. The normal files form two
byte-identical main/candidate pairs. The controller author also performed a
separate artifact audit; that role is disclosed, alongside the independent
reviewer who did not author production, oracle or controller code. Neither
reviewer reran adapters, tests or study workers. See the
[final review](evidence/exact-ingress-20261111/final-review.json).

## Reproduction and retained artifacts

With project development dependencies and Python 3.12, verify both archived
sources in a new evidence directory:

```bash
python tools/reproduce_exact_ingress.py --stage verify \
  --candidate "$(git rev-parse HEAD)" \
  --output experiments/exact-ingress-independent/verify
```

The original local study used:

```bash
python tools/reproduce_exact_ingress.py --stage baseline \
  --output experiments/exact-ingress-20261111/baseline
python tools/reproduce_exact_ingress.py --stage study \
  --candidate 238843475e757b8efec0f0a7feffd4cb511e2c10 \
  --baseline-evidence experiments/exact-ingress-20261111/baseline \
  --output experiments/exact-ingress-20261111/study-final
```

The controller records actual imported file paths and hashes from isolated Git
archives, dependencies, raw stdout, JUnit, source pins and complete evidence
manifests, including stopped runs. It refuses to overwrite retained evidence.
The local interpreter was
`/workspace/lob-market-maker-accounting-fix/.venv/bin/python` (Python 3.12.14).
Because that interpreter has an older editable installation, direct full-suite
verification explicitly used `PYTHONPATH=$PWD/src`; the controller checks its
own archived imports. All 37 actual full-suite module paths/hashes were checked.

Original local evidence remains under
`/workspace/lob-market-maker-ingress-fix/experiments/exact-ingress-20261111/`.
The [workspace manifest](evidence/exact-ingress-20261111/workspace-manifest.json)
lists original files except bytecode caches. The committed
[local evidence](evidence/exact-ingress-20261111/local/) retains verbatim
records, generated CSVs, actual canonical Parquets, logs, JUnit, controls,
context snapshots, environment and quality results. Redundant archived source,
input and harness copies remain locally and are represented by hashes and Git
commits. The [publication manifest](evidence/exact-ingress-20261111/sha256-manifest.json)
hashes the report, workflow, source, frozen harness and curated evidence.

Local ordinary verification passed **258 tests with 90.63% branch-aware
coverage**, Ruff lint/format (83 files) and strict mypy (37 source files).
After the full suite finished, the old execution-session identifier was no
longer available to retrieve its shell exit status. Completion was verified
from the finished zero-failure/error/skip JUnit, terminal pytest/coverage log and
post-session import record; the suite was not rerun. Existing integration tests
and the ordinary CI smoke are compatibility verification, not strategy trials.

Exact-head CI runs ordinary quality/smoke checks plus the complete two-source
ingress verification and uploads head-named evidence for 14 days. Its verified
terminal run, executed commit and artifact metadata belong in the final draft
PR body; local success alone is not remote CI verification. Remote ZIP bytes
are not implied independently audited by metadata or automated comparisons.
