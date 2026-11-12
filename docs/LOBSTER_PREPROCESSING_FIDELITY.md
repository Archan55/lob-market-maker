# LOBSTER preprocessing fidelity — November 12, 2026

LOBSTER preprocessing still loses valid source information after PR9. At
Decimal precision 3, a faithful integer-price pair is admitted with bid/ask ticks
`10000/10100` instead of `10001/10103`; a nanosecond-grid pair's 1 ns and 9 ns
offsets both become zero. Both incorrect streams pass strict canonical
validation. At default precision, the deliberately invalid long-subnanosecond
input is also admitted after rounding hides its fractional nanosecond.

The single candidate composes price scaling/ticks and seconds/nanoseconds through
PR9's unchanged exact kernel. All 36 frozen observations pass, including exact
TRADE cause, and the ordinary default-precision output remains byte-identical.

Ordinary valid LOBSTER inputs are not predicted to fail at default precision 28:
Int64 raw prices and within-day nanosecond times fit that precision. Extra
subnanosecond digits are invalid-source rejection controls, not valid vendor
observations or evidence of routine default-precision corruption.

## Frozen scope

The comparator is completed PR9
`253abee735c02d7805e2a4377c12626742108d84`. The new branch is stacked on
`fix/exact-ingress-conversion` and requires its unchanged exact integer kernel.
Only `src/lobmm/data/lobster.py` may change among the 38 production paths.
Existing PR9 code, tests, controller, frozen inputs, evidence and report remain
preserved at their original bytes/commit. Their manifests are historical
snapshots at that commit, not live manifests for later workflow/document edits.

The [contract](evidence/lobster-fidelity-20261112/contract.json),
[paired inputs](evidence/lobster-fidelity-20261112/fixtures.json) and
[independent design review](evidence/lobster-fidelity-20261112/design-review.json)
were committed as `88510ac5190487e13810cf4eb9fdbc86e0944fe0` before every new
numerical probe, oracle evaluation, collection, test, conversion and candidate.
The independent oracle/controller and final static acceptance were frozen in
`0aab4ce857859ab20523bcdd7cd0086935ca102f` before baseline execution.

Twelve fixed paired-file cases run in three fixed contexts: precision 28,
precision 3, and precision 3 with Inexact/Rounded traps. The 36 public adapter
observations cover isolated prices, isolated nanosecond time, their combination,
off-configured-tick book/message prices, short/long subnanosecond rejection,
backward source time, zero-size dummy levels and day/zero boundaries. Positive
cases use integer raw prices, the standard scale 10,000 and nanosecond-grid
times. Negative cases remain explicitly labeled. No real vendor data is used.

Each two-row positive pair has a first submission represented once by
RESET/SNAPSHOT and then a visible BID execution of one unit, with the paired
book decreasing from two units to one. Exact event type is required: correcting
book prices alone while leaving message prices rounded could turn that TRADE
into a reconciliation CANCEL. A strict book certificate alone cannot establish
source fidelity.

The independent oracle derives prices as the single rational expression
`raw_price / (scale * tick)` and timestamps as exact seconds times one billion
plus an independently computed integer midnight. It never uses float epoch
conversion or the production conversion/calendar helpers. The time pair has
1 ns and 9 ns offsets after 34,200 seconds, an 8 ns gap. Primary cases configure
the supported UTC timezone to isolate arithmetic; the existing normal control
uses default America/New_York. Calendar/DST behavior is not a new study.

## Candidate, criteria and stopping rule

The sole candidate may compose integer coefficients/exponents through PR9's
unchanged kernel. No intermediate Decimal product or quotient is allowed for
prices/time, including error formatting. Error class, field, row and reason
remain stable; price messages may display exact raw/scale/tick operands rather
than the old rounded display quotient. Quantities, calendar/timezone, parsing
order, zero-size dummy handling, first-row validation, hidden/cross/halt rules
and canonical ordering rules stay unchanged. No new raw-integer-only policy is
introduced for the adapter's general finite numeric API.

Advance requires actual admitted, strict-valid but oracle-wrong reduced-precision
**valid-format** price and time pairs on PR9. Default-precision counterparts must
be correct. The explicitly invalid long-subnanosecond case is separately
predicted to be wrongly admitted at default precision. No fallback case or
favorable input substitution is allowed.

All candidate rows, causes, times, prices, quantities, error reasons, raw input
observations and context/trap/flag/guard facts must match the frozen contract.
The existing fictional LOBSTER control at default precision must preserve actual
Parquet bytes, rows/schema, diagnostics, event fingerprint and strict certificate.
Its previously exposed rows are not a fresh holdout. Existing PR9 tests repeat
only in the ordinary regression suite; the closed prior source study is not
reopened or reselected.

The local budget is one PR9 worker, then one candidate worker reusing and
rehashing its evidence, each with a 120-second cap. At most one independently
justified source repair is allowed with unchanged inputs/gates. Stop on an
unresolved primary reproduction, oracle, provenance, quality or budget failure.
Full-suite coverage (at least 90%), Ruff, strict mypy, independent source/actual
artifact review and terminal exact-head CI are required before completion.

## Interpretation

This is synthetic data-ingress correctness, not market-making profitability or
calibrated execution evidence. It does not admit real LOBSTER data, establish
data-use rights, true queue position, fills or realistic latency/costs. Those
prerequisites and PR3's performance/output-growth limits remain unresolved.
The stopped November 2/4 candidates, main and PR1–9 remain preserved. No data
acquisition, Nasdaq review, private E1/E2 material, denied-action retry, paid
service, credential/access/security change, desktop, merge, deployment or trading
is included.

## Results and decision

The measured candidate is `6b619fc992ead607feedccbf1bd590db2c60e01b`.
Relative to PR9, only `lobster.py` differs among the 38 pinned production paths.
The exact kernel, adapter utilities and all 251 protected historical PR9 files
remain unchanged. Later documentation/evidence edits do not alter measured
source, inputs, oracle or controller.

| Fixed context | PR9 observations with semantic mismatch | PR9 observations with changed flags | Candidate, either category |
| --- | ---: | ---: | ---: |
| Precision 28 | 1 | 1 | 0 |
| Precision 3 | 9 | 9 | 0 |
| Precision 3 with traps | 9 | 9 | 0 |
| Total over 36 observations | 19 | 19 | 0 |

The categories overlap; these are 19 observations exposing preprocessing
rounding, not 19 distinct defects. PR9 changes Inexact/Rounded flags or raises
their trap exceptions. Neither source changes the configured precision,
rounding mode, other settings, outer context or integer-string guard. The
candidate preserves every recorded context/trap/preexisting-flag value.

The valid reduced-precision price witness retains the original integer CSV
values and coherent post-message book, but PR9's intermediate display-price
division changes the resulting ticks. The valid time witness collapses an
8 ns gap to zero. The candidate preserves both exact prices and offsets. Both
book and message conversion are corrected: all two-row positive cases retain
the one-unit BID TRADE and have no window-reconciliation cancellation/addition.
The oracle would reject a partial book-only repair that silently changed that
TRADE into a reconciliation CANCEL.

Default-precision valid controls already match the exact oracle. No numeric
corruption of ordinary valid default-precision vendor-style inputs is shown.
The default long-subnanosecond admission is instead a demonstrated invalid-input
rejection hole. Candidate negatives raise `MarketDataAdapterError` with the
frozen field, row and reason, without creating a canonical file. Error price
text intentionally displays original raw/scale/tick operands; it does not
recompute a rounded display quotient merely for diagnostics.

All 15 candidate positive frames match the independent exact rows, causes,
diagnostics, dictionary reconstruction, fingerprints and strict certificates.
All 21 negative observations have the required errors. The ordinary 17-row
control's actual Parquet bytes, rows/schema, diagnostics, fingerprints and
certificate equal the PR9 baseline. Its previously exposed input is a regression
control, not a fresh market holdout.

The practical decision is to retain the single-file correction in a draft
stacked on PR9, preserving that dependency rather than duplicating its kernel.
It closes the demonstrated upstream price/time loss path. This establishes
source-conversion fidelity for the bounded study, not real-data rights,
venue-faithful order priority, execution calibration or economic advantage.

## Trials, provenance and independent review

Exactly one PR9 worker and one candidate worker executed locally, each with
the same 36 public pair observations and one existing normal conversion.
Candidate comparison rehashed and reused baseline evidence without rerunning
PR9. There was one source candidate, zero numerical repairs, no omitted trials,
timeouts, worker infrastructure failures, substituted pairs or changed gates.
Full-suite and exact-head CI repetitions are separately prescribed verification.
Existing PR9's 240-case corpus runs only as an unchanged ordinary regression;
its closed historical experiment is not reopened.

One metadata-path omission was found and corrected during static prebaseline
review: the preservation list also needed PR9's narrative report. That happened
before numerical execution and changed no inputs or criteria. Static authoring
and linting are not hidden numerical candidates.

| Frozen component | SHA-256 |
| --- | --- |
| Contract | `513f9b2be85b582c020e575b32dded6df123f10d0aaeee4e51d7aceed755de8b` |
| Paired inputs | `1987016a419c065535b2ac5590305e1966328c39e495feda8eb487dbf2dbf670` |
| Independent test/provider | `a29f7842ff5565f7a9f80b46544333208684a251fc192cbd19c756dad6e76906` |
| Controller | `57ddf8ed05df7fd2b161bb111920eeb72f146e3162dcb42cb158819d1e10a8ee` |
| Candidate LOBSTER source | `145922912df0c4c4826ff45d48918cbfdc05d9c03a94d499bf40528c79202ce5` |

Independent reviewers challenged domain faithfulness, protocol, oracle and
controller before outcomes, then the actual source before candidate measurement.
Actual-result review checked the complete original-input and context ledgers,
raw boundary-call prefixes and copied level states. All 34 actual Parquets
(18 PR9, 16 candidate) were independently rehashed/decoded/reconstructed and
fingerprinted. The 148 observed CSV files comprise 144 generated paired files
and four copies of the two existing control inputs. The normal pair is
byte-identical. Archive/import/source pins and all 251 historical PR9 file bytes
were checked against the pinned snapshot. The controller author's separate
artifact audit is disclosed alongside the reviewer who authored no production,
test or controller code. Auditors did not rerun adapters, tests or study workers.
See the [final review](evidence/lobster-fidelity-20261112/final-review.json).

## Reproduction and evidence

With project development dependencies and Python 3.12, use a new output path:

```bash
python tools/reproduce_lobster_fidelity.py --stage verify \
  --candidate "$(git rev-parse HEAD)" \
  --output experiments/lobster-fidelity-independent/verify
```

The original local commands were:

```bash
python tools/reproduce_lobster_fidelity.py --stage baseline \
  --output experiments/lobster-fidelity-20261112/baseline
python tools/reproduce_lobster_fidelity.py --stage study \
  --candidate 6b619fc992ead607feedccbf1bd590db2c60e01b \
  --baseline-evidence experiments/lobster-fidelity-20261112/baseline \
  --output experiments/lobster-fidelity-20261112/study-final
```

The controller refuses to overwrite retained evidence, uses isolated Git source
archives and verifies actual imported paths/hashes, frozen inputs, dependencies,
JUnit and complete file manifests, including stopped attempts. Historical PR9
manifests are checked as preserved files at their original commit; their old
live-code/document entries are not reinterpreted as current-head snapshots.

The local interpreter was
`/workspace/lob-market-maker-accounting-fix/.venv/bin/python` (Python 3.12.14).
Direct suite verification used `PYTHONPATH=$PWD/src` to supersede its older
editable installation. All 37 actual suite module paths/hashes were checked;
the suite's identity observer runs after coverage has observed execution.

The original evidence remains under
`/workspace/lob-market-maker-lobster-fix/experiments/lobster-fidelity-20261112/`.
The [workspace manifest](evidence/lobster-fidelity-20261112/workspace-manifest.json)
lists its files except bytecode caches. The committed
[local evidence](evidence/lobster-fidelity-20261112/local/) preserves verbatim
records, actual CSV/Parquet files, raw observer/context/oracle facts, logs,
JUnit, environments and quality results. Redundant source/frozen-input/controller
copies remain locally and are represented by source hashes and Git commits.
The [publication manifest](evidence/lobster-fidelity-20261112/sha256-manifest.json)
hashes the report, live adapter documentation, workflow, source, frozen harness
and all curated evidence.

Local ordinary checks passed **262 tests / 90.72% branch-aware coverage**,
Ruff lint/format (86 files) and strict mypy (37 source files). This includes the
unchanged PR9 regression tests. Existing integration tests and CI smoke are
compatibility checks, not strategy-selection trials or market-performance data.

Exact-head CI records the actual checkout, runs ordinary gates/smoke and both
archived source workers, prints complete numerical/source ledgers and retains
head-named artifacts for 14 days. Verified terminal CI/run/head/artifact details
will be recorded in the draft PR body after publication; local results alone
are not remote CI proof. Metadata and automated comparisons do not imply an
independent download/audit of remote ZIP or Parquet bytes.
