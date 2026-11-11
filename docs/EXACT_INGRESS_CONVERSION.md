# Exact strict ingress conversion — November 11, 2026

This is a preregistered, unexecuted study of a source-ingress validation lead.
The current helpers divide or multiply Decimal operands under the caller's
context before checking whether the result is an integer. The documented
contract instead requires rejecting nonrepresentable source prices and amounts.
The hypothesis is that rounding can erase a fractional remainder before that
check, even when the resulting tick/quantity is an ordinary small integer.
No runtime defect or candidate result is claimed at this stage.

## Frozen question and scope

The comparator is main `1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`.
The same conversion source is present in PR8 and the published PR3/4 heads;
Portfolio cost-basis and risk-threshold work did not change ingress validation.
The [contract](evidence/exact-ingress-20261111/contract.json) and
[fixed inputs](evidence/exact-ingress-20261111/fixtures.json) precede any new
numerical probe, oracle evaluation, test execution, CSV conversion or candidate.

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

One candidate may replace only strict price/quantity arithmetic with exact
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

Results, trial accounting, reproduction commands and verified publication/CI
links will be added only after the frozen study is executed and reviewed.
