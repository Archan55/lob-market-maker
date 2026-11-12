# LOBSTER preprocessing fidelity — November 12, 2026

This preregistered study has not executed any new numerical case yet. PR9
corrected strict shared conversion helpers, but LOBSTER still divides raw prices
by the vendor scale and multiplies seconds by one billion before those exact
checks. The hypothesis is that these intermediate Decimal operations can lose
valid integer-price or nanosecond-time information under reduced precision.

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
precede every new numerical probe, oracle evaluation, collection, test,
conversion and candidate.

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

Results and complete trial/source/artifact provenance will be recorded only
after the frozen baseline and candidate are executed and independently reviewed.
