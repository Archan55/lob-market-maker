# Positive-price quote construction — November 9, 2026

All three strategy policies abort at flat inventory on a valid bid 1 / ask 2 book
with the default half-spread: the rounded bid is zero and `DesiredQuote` rejects
it before the strategy can return ask 3 or handle its managed orders. The frozen experiment
reproduced this on main and verified a six-condition correction: omit computed
nonpositive sides. The candidate completes every constructed low-price replay
while retaining identical outputs on the ordinary controls.

The policy under test omits only a side with a computed price of zero or below.
Every surviving target retains the original reservation formula, rounding,
size, inventory permission and tuple order. Prices are not clipped to one or
projected to the observed BBO. Positive targets that cross the observed book
retain their current meaning; post-only requests are still validated at venue
arrival. This is a construction-domain correction, not a strategy-performance
or optimal-pricing claim.

## Frozen study

The [contract](evidence/positive-price-20261109/contract.json),
[inputs](evidence/positive-price-20261109/fixtures.json) and
[independent design review](evidence/positive-price-20261109/design-review.json)
were committed as `d8f4a8192d7595118c48592601db7807a203eb2a` before any new
policy test, tape validation, backtest, candidate or outcome inspection.
The source comparator is main
`1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`. Sixteen production module hashes
are pinned; only the three strategy policy files may change.

Nine named pure-policy contexts cover the default low book, positive price-one
boundary, ordinary prices, signed inventory and cap endpoints, nonpositive ask
targets where the policy permits them, and the minimum configured half-spread.
They run for all three policies and both post-only modes. An independent oracle
computes the original prices, sizing and inventory permissions before applying
the representability condition. FixedSpread ignores inventory penalty, so the
strong-skew context is intentionally not expected to have the same result for
every policy.

A managed-state control starts with accepted bid 1 and ask 3, then tightens the
book to bid 1 / ask 2. The intended omission routes the existing bid through the
current cancellation path despite minimum lifetime, retaining unchanged ask 3;
a repeated decision must not duplicate a pending cancel. This uses public
strategy/report operations, not a claim that a venue cancel completed.

The one full-engine tape starts with a valid bid 1 / ask 3 plus deeper ask 4.
Adding ask 2 at time 10 reaches the inadmissible bid target after both initial
quotes were accepted. A later add at ask 3 goes behind the resting own ask. A
trade removes ask 2 at time 30; a two-unit trade at ask 3 at time 40 consumes
one external unit ahead and the one-unit own order. The remaining external unit
was added behind it. Deeper ask 4 keeps the historical book two-sided. The
predefined candidate outcome is one maker ask fill at 3, no buys and final
inventory −1. This is a purpose-built strict mechanics witness.

All three policies run the same tape with the additional inventory, imbalance
and volatility coefficients set to zero. Microprice's fair value remains its
observed microprice; later re-entry times and action counts may legitimately
differ. Zero channel delays, zero fees and the short logical timeline isolate
construction and queue behavior. They are not calibrated venue assumptions.

Three ordinary CI300 comparisons retain the existing seed/configuration and
default signals, changing only the selected policy name. Equivalence is tested
within each policy, not used to rank the policies. These inputs were already
exposed in prior work. No new 20k replay, timing search or retained-market
analysis is planned.

There is one omission candidate and at most one independently justified
correctness repair. The local main baseline is executed once and rehashed for
candidate comparison rather than rerun. The budget is three low-tape attempts
and three ordinary replays per source, plus the frozen pure/managed corpus.
Ordinary tests and exact-head CI are separately disclosed verification.

## Interpretation and compatibility

`DesiredQuote`, request, canonical-event and venue validators stay unchanged.
The same price-domain omission applies in both post-only modes; it does not
turn post-only rejection into an economic error. A missing desired side uses
the existing quote-manager path. Its legacy `risk_side_suppression` label means
the desired side is absent; it does not identify why it is absent, prove a
RiskManager rejection or establish cancellation completion/liveness.

Known historical order exports repeat some terminal queue/cumulative fields.
That limitation was already documented in PR1. The experiment uses independent
raw-event/queue accounting and contemporaneous observer copies rather than
treating those columns as trajectory proof. No export-history or recovery
redesign is included.

## Results and practical decision

The source implementation measured locally is
`2fc983cb861697a9cab4da00f9f3948b2066da42`. Subsequent report/evidence changes
do not alter the measured source, inputs, tests, observer or controller.

| Frozen comparison | Main | Candidate |
| --- | --- | --- |
| Same 64-node boundary/managed corpus | 26 fail, 38 pass; no errors/skips | 64 pass; no errors/skips |
| Three low-price full-engine attempts | All abort at time 10 constructing bid 0 | All complete with the predefined one-unit maker sell at price 3 at time 40 |
| Three ordinary CI300 controls | Eight tables per policy | All 24 within-policy table pairs byte-identical |

The 26 failed parameterizations are manifestations of this price-domain issue,
not 26 independent defects. The separate ledgers retain all 54 pure-policy,
six managed-state and 18 validator probes. Both post-only modes, positive price
one, inventory limits, rounding and sizing retain their predefined behavior.
Positive targets crossing the observed BBO remain positive targets; the study
does not impose a new pricing policy. An empty tuple is legitimate when every
permitted computed target is outside the positive domain.

In every low tape, both original orders are accepted at time 0. At time 10 main
raises before emitting a command. The candidate instead omits bid 0, issues the
original bid's single cancellation before minimum lifetime, and retains the
original ask's complete managed state. The zero-delay tape then delivers an
actual cancellation report for the unfilled bid. Independently copied queues
show one external unit ahead of the original ask and one behind it. The time-40
trade fills exactly that one-unit original maker ask at price 3. There are no
buys or risk events, and terminal inventory is −1. The separate managed probe
retains pending orders until a report; it does not invent acknowledgement.
These observations establish this tape's transition and fill, not general
cancellation liveness or historical recovery.

All three ordinary comparisons also preserve resolved configuration, input
event identity, summary, full metrics and diagnostics after excluding only the
three declared wall-clock fields. There is no ordinary-control performance or
economic improvement claim. Main's low runs are failed attempts with retained
partial traces, not completed backtests with invented output tables.

The practical decision is to keep the narrow omission correction reviewable in
an independent draft against main. It removes a valid-input construction abort
without repricing surviving quotes or changing order/venue validation. This
study does not select a profitable policy, calibrate fills, optimize low-price
quoting or resolve pending-intent recovery.

## Provenance, trial accounting and independent review

The protocol was frozen in `d8f4a8192d7595118c48592601db7807a203eb2a`.
Independent oracle/provider/controller and preexecution acceptance review were
frozen in `1ba9c78acc080be67e2aa7b905684cf34a3662fc` before baseline execution.
The following hashes stayed unchanged through all local measurements:

| Input or implementation | SHA-256 |
| --- | --- |
| Contract | `8862b5a2d346e65248e8a561c488dfe3c2bdf1b85d54de75a85dd85fe017c037` |
| Fixtures | `96c30af69e182c3218c9f892ac5276f773b3a10393adf0782441869470cf727c` |
| Independent test | `40cc751b121c0c23668f6cfd2c42dbf2962a8e210e21634e4734cb8f8614d4c1` |
| Contemporaneous witness provider | `e8ab1ed2e1de58687233fa8b3cf1dc692e4b7d9a62645d9bda5392895af773a8` |
| Reproduction controller | `28330785551ada2e1f243b14538d7d8cf3e264d461108d35e05e045415e59b53` |
| Candidate fixed-spread policy | `e79c4a946bc60065dff9f7adf1d386deeea3e772d67f4b4ac6cc84ed8e8be72c` |
| Candidate inventory-aware policy | `ffab8e34ccf75f48b0f278efe6c16fd1e85fb81bc8044a53b2e248075874d972` |
| Candidate microprice policy | `9f62d11eb33c91f535be76db90653dfd7632d4ce141e50ae1c2a799747f7a4aa` |

The original main baseline ran once: one corpus/probe worker, three expected
failed low-tape attempts and three ordinary replays. The candidate ran once:
one corpus/probe worker, three completed low-tape replays and three ordinary
replays, reusing and rehashing the retained baseline. That is 14 workers and
12 small engine attempts. One candidate was used, with zero repairs, omitted
trials, timeout/infrastructure failures or changed gates. CI replication and
ordinary suite verification are separate from this candidate-selection budget.
No benchmark timings or new 20k/historical periods were inspected. The static
lead was selected after rejecting an observed-BBO clamp as a different pricing
policy and recognizing terminal order-export limitations as already documented
in PR1. The stopped November 2/4 candidates remain stopped.

Independent reviewers challenged the design before execution and then audited
actual source/import/archive hashes, JUnit node identities, frozen inputs and
all worker ledgers. A separate artifact reviewer rehashed and decoded every
actual Parquet: 48 normal files forming 24 identical pairs and 24 candidate low
files. The reviewer independently checked raw book/FIFO accounting,
contemporaneous manager/queue copies, complete ask retention, bid cancellation
and the original-order fill. Neither reviewer reran or repaired the measured
study. [Final review](evidence/positive-price-20261109/final-review.json) records
coverage and publication gates.

## Reproduction and retained deliverables

Run from this checkout with the project's development dependencies and Python
3.12. The controller archives the pinned Git sources into separate directories,
checks actual imported module paths/hashes and refuses to overwrite retained
evidence.
For an independent verification of both sources:

```bash
python tools/reproduce_positive_price.py --stage verify \
  --candidate "$(git rev-parse HEAD)" \
  --output experiments/positive-price-independent/verify
```

The original local two-stage commands were:

```bash
python tools/reproduce_positive_price.py --stage baseline \
  --output experiments/positive-price-20261109/baseline
python tools/reproduce_positive_price.py --stage study \
  --candidate 2fc983cb861697a9cab4da00f9f3948b2066da42 \
  --baseline-evidence experiments/positive-price-20261109/baseline \
  --output experiments/positive-price-20261109/study-final
```

The cloud interpreter was
`/workspace/lob-market-maker-accounting-fix/.venv/bin/python`, Python 3.12.14.
That environment has an older editable installation, so direct pytest/CLI
verification explicitly used `PYTHONPATH=$PWD/src`; the reproducer checks its
archived imports independently. Dependencies and all 16 source hashes are
retained with each source's evidence.

The complete original local workspace evidence remains at
`/workspace/lob-market-maker-price-domain/experiments/positive-price-20261109/`.
The [workspace manifest](evidence/positive-price-20261109/workspace-manifest.json)
records all original files except bytecode caches. The committed
[local evidence](evidence/positive-price-20261109/local/) includes verbatim
worker JSON/logs, JUnit, probes, low input/oracle/transition traces, all 72
Parquets and their metadata, environment records and full-suite results.
Redundant archived source copies and harness copies are represented by their
hashes and Git commits rather than duplicated in that directory. The
[publication manifest](evidence/positive-price-20261109/sha256-manifest.json)
hashes the committed report, methodology, workflow, source, harness and evidence.

Local verification: Ruff lint/format passed (84 files), strict mypy passed
(37 source files), and the full suite passed **314 tests with 90.68% branch-aware
coverage**. The collection-time identity observer records actual imported
module paths/hashes without preimporting source before coverage. These suite
results are compatibility checks, not research progress measured by test count.
Exact-head CI separately runs lint, format, typing, coverage, deterministic CLI
smoke and the complete archived-main/candidate verification; its terminal run,
executed head and retained artifact link will be recorded in the draft PR body
after publication. Local results do not claim a remote stage has already run.

## Remaining decision prerequisites

[Metadata-only review](evidence/positive-price-20261109/data-feasibility.json)
identified no new legally usable real L2/trade tape or execution calibration.
Existing inputs remain synthetic or explicitly fictional. Economic strategy
evaluation still requires multi-session data with reconstruction and clock
evidence, dated instrument/cost schedules, and defensible queue, priority and
order/cancel/fill/report latency calibration. This boundary study cannot resolve
those prerequisites or PR3's accounting overhead/output-growth limits.

Main, PR1–7 and the stopped November 2/4 performance candidates are preserved.
The new branch is an independent refinement against main, not combined
integration of those drafts. No merge, deployment, live trading, paid services,
new acquisition, access/security change, private/licensed E1 input or desktop
execution is involved.
