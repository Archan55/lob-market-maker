# Conserved incoming orders and counterfactual book paths

A standalone aggregate-order matcher and an independently implemented unit-token
reference reproduced the complete manual oracle across the frozen family.
The useful added witnesses concern **missing incoming size and divergent future
book state**, rather than rediscovering the supplied five-unit hand example:

- The same executed print of six units can coexist with incoming size six or
  eleven. Counterfactual own fill is respectively zero or five. Executed prints
  plus taker ID alone cannot distinguish these possibilities.
- In the two-incoming carry case, retaining historical external fills produces
  signed event-budget errors `+5, -5`. They cancel in terminal totals while both
  incoming executions are wrong. Displaced lower liquidity remains available in
  the counterfactual book and can execute against later incoming flow.
- Full traces are essential: zero hybrid quantity error does not establish
  correct price/time priority. Limited depth can also turn previously unfilled
  size into own fills without reducing total external fills.

These are preregistered, constructed mechanics checks with expected outcomes.
They are not discoveries about a venue, real-fill calibration, a production
replacement or evidence of economic advantage. Production source is unchanged.

## Contract, provenance and exposure

[The design](INCOMING_REPLAY_DESIGN.md) and manual construction source were
committed at `a3ffd68`; the explicit family was committed at `3f543ef` before any
matching. Pre-outcome normalization through `51f89a0` preserved identical case
bytes. [Cases SHA-256](evidence/incoming-order-20261120/cases.json):
`23b6f928ae94609decfceb86e1c435f8806bb05fb21fe2eb8baf8e9b1efb1b2e`.
Expected snapshots are arithmetic subtraction of literal manual fill vectors
from activations, not outputs of either matcher.

The aggregate matcher and controls were sealed at `c067de5`; independent token
source was frozen earlier at `0851745`. The [complete plan](evidence/incoming-order-20261120/plan.json)
was committed at `a487e18b9ab5efc5b4aa12676697eefb4cb7ac5b` **before even the
first preflight matching controls**. Plan SHA-256:
`6a12d18cf6ad84aaa83dadc25d0c67fae15c3f9b30e30d84679999efed4434f1`.

Base is PR16 `2a96738c080b1a62a32e4306721de703b242477a`. All 38 production
files remain PR13 C1 `0866bf13e449cd66e3862454eaaf424a5172c33a`.
[Prerequisites](evidence/incoming-order-20261120/prerequisites.json) record all
sixteen unchanged drafts, empty feedback/issues and successful existing
head-associated CI runs. Main and earlier heads are preserved.

There are 18 manually designed sell/bid recipes and their price-mirrored buy/ask
controls (`p -> 202-p`): **36 paired scenarios, 72 world paths and 80 incoming
walks**. Two recipes are the supplied prior-exposed Q11/Q8 examples; including
their mirrors gives four labeled exposed control scenarios. All mirrors and
all other cases are constructed engineering checks, not independent samples or
fresh holdouts. The 37 preflight software controls overlap some declared
behaviors; no holdout claim follows from their later family reproduction.

The model uses one consumed side, noncrossing resting activations by construction,
fully displayed foreign-owned makers and explicit known base quantities. Atomic
incoming IOC walks use price then original activation `(timestamp, sequence)`
priority; partial makers keep their priority. Unfilled size terminates. Only
listed activations occur between incoming orders, whose later flow is held fixed
in both worlds. Same-time sequence controls are synthetic chronology, not the
production scheduler's or a venue's timestamp convention.

Hidden/reserve liquidity, iceberg replenishment, cancellations/interleaving
during a walk, STP, funds constraints, venue protection bands, auctions and other
TIF behavior are excluded. Two explicit hard-limit cases are included. No fees,
cost-sensitive funds limit, marking, P&L or strategic response is evaluated.

The [official channel documentation](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels)
and [Trading Rules](https://www.coinbase.com/legal/trading_rules) were checked on
November 20 for reconstruction/identifier and price-display-time context. Full
order/sequence evidence is stronger than trade grouping; iceberg, funds and
protection details require additional modeling. The design records the precise
scope. No Coinbase market data, data-use permission or live connection was
acquired. No separate Hyperliquid admission was attempted in this pass.

## Operator and exhaustive checks

Baseline and counterfactual allocate separate books and receive the same complete
incoming stream. Baseline skips own activations; counterfactual includes them.
The primary spends one mutable quantity budget over active eligible maker orders.
The reference expands each maker into individual displayed-unit tokens, selects
the eligible priority prefix, removes those tokens and coalesces execution rows.
Neither implementation consults the expected subtree to determine executions.

Every event snapshot, ordered fill vector, IOC residual and final maker balance
matches the frozen oracle. All 360 snapshots, 168 execution rows, 72 final world
states and 80 incoming walks were independently checked. Maker identity and
original priority persist after partial fills; late activation is processed even
after the final incoming event. No input mutation or cross-world state sharing
was found.

Every actual incoming satisfies `Q = external + own + unfilled`. Every activated
maker satisfies `entered = cumulative fills + remaining`. Each paired incoming
also satisfies:

```text
CF own = (baseline external - CF external)
       + (baseline unfilled - CF unfilled)
```

Both differences are signed. Own fills need not equal only displaced historical
executions, and per-event external displacement need not remain nonnegative.

## Added adverse and structural results

All listed outcomes occur on both price mirrors:

| Frozen check | Observed mechanics |
|---|---|
| Shallow incoming-size ambiguity | Baseline print E1:6 is identical; Q6 has baseline residual 0 and CF own 0, while Q11 has baseline residual 5 and CF own 5. Whole baseline traces and a complete order feed are not claimed identical. |
| Arrival placement | Same aggregate external top depth 6 gives own 2 or 4 depending on which external units arrived before the inserted order. Full activation histories differ. |
| Same-time sequence | Activation before the incoming event gives own 2; after it gives own 0 and later remaining 5. |
| Late activation | An incoming IOC leaves 5 unfilled; later own activation rests 5 and cannot resurrect that incoming residual. |
| Two own makers | A single Q11 after external 6 fills Oa:3 then Ob:2, leaving Ob:2. The incoming budget is not duplicated. |
| Depth exhaustion | Q17 fills external 11 and own 5, leaving 1 unfilled; baseline has external 11 and 6 unfilled. Own size replaces residual, not external quantity. |
| Eligible hard limit | Sell limit 100 gives baseline external 8/residual 3; CF external 6/own 5/residual 0. |
| Excluded own limit | Sell limit 101 excludes the own maker at 100; both paths execute external 6 with residual 5. |

In displaced_carry, the first incoming Q11 consumes external 6 plus own 5 in the
counterfactual, leaving lower external orders untouched. The subsequent Q5 then
consumes those external 2+3. Baseline is already empty and leaves that Q5 unfilled.
Future flow is fixed by construction; no unchanged real future path is inferred.

The deliberately invalid hybrid copies baseline external executions and adds CF
own fills with CF residual. Its signed error is `baseline external - CF external`:

| Incoming in displaced_carry | Baseline external | CF external | CF own | Hybrid budget error |
|---|---:|---:|---:|---:|
| First, Q11 | 11 | 6 | 5 | +5 |
| Second, Q5 | 0 | 5 | 0 | -5 |
| Terminal sum | 11 | 11 | 5 | 0 |

Across the 40 paired incoming rows, expected hybrid errors are positive on 24,
negative on 2 and zero on 14. This is a negative-control diagnostic, not a third
matching world or a production defect. Zero-error rows are not blanket passes:
in exhausted17, appending own after all historical external executions has the
wrong priority despite conserving total quantity. In shallow11 the hybrid can
coincide with the correct path. Full oracle paths supply the stronger criterion.

The known Q11/Q8 controls reproduced own 5/2 as expected. For Q11, five own units
can legitimately replace the historical lower prints 2+3; capping own size by a
single lower print is not the operation tested here. Those known results and
all added witnesses were predicted before execution, not selected afterward.

## Trial accounting, review and evidence

One primary isolated fixed-family pass completed all 36 paired scenarios, with
zero unexpected criterion failures, retries, repairs, changed cases or added
trials. The 37 fixed controls passed first after the execution plan was sealed.
Static constructor/style and loop-binding corrections preceded every matching
outcome; fixture regeneration after style normalization was byte-identical.
One frozen independent token-reference pass then corroborated the same family.
No strategy, production engine or resource worker generated primary outcomes.

The primary controller recorded 0.234821976 seconds and worker 0.215989226
seconds, under the 180/120-second caps. These are experiment-budget records,
not throughput or production-capacity measurements. The complete raw run has
47 inventoried files plus manifest: 48 files and 543,902 bytes, below 16 MiB.
Runtime/import records confirm the research worker imported no application code.

Independent design review checked every literal oracle, mirror and source/control
contract before execution. The unit-token implementation matched all traces,
states and 40 paired records; a separate physical-artifact reviewer independently
checked priority, eligibility, per-event/order conservation, oracle comparisons,
hashes and witnesses without invoking either matcher. These are independent
assistant reviews, not external human review or extra market observations.

Complete deliverables:

- [Manual constructor](evidence/incoming-order-20261120/build_cases.py),
  [frozen cases](evidence/incoming-order-20261120/cases.json) and
  [plan](evidence/incoming-order-20261120/plan.json).
- [Original manifest](evidence/incoming-order-20261120/run/manifest.json),
  [summary](evidence/incoming-order-20261120/run/worker/summary.json),
  [all 36 case artifacts](evidence/incoming-order-20261120/run/worker/cases/),
  [40 paired records](evidence/incoming-order-20261120/run/worker/paired.jsonl).
- [Frozen token reference](evidence/incoming-order-20261120/independent-review/reference.py),
  [result](evidence/incoming-order-20261120/independent-review/result.json),
  [independent review](evidence/incoming-order-20261120/independent-review/REVIEW.md)
  and [physical publication review](evidence/incoming-order-20261120/PUBLICATION_REVIEW.md).
- [Integrity receipt](evidence/incoming-order-20261120/integrity-verification.json),
  [local source proof](evidence/incoming-order-20261120/local-source-proof.json) and
  [validation/log index](evidence/incoming-order-20261120/local-validation.json).

The independent reference source/result SHA-256 pins are
`b587eab4d53e313d0a8b294e9e28fc599f74291b7f8de970fc51421e072d6a4a` /
`b7172bf85f6930a8af5e43d4492a21c2e0cdb4255b2ebe080b48f9b331a765e7`.
Summary case hashes use canonical compact JSON objects; manifest hashes use raw
pretty-JSON file bytes. They intentionally differ. No matching rerun or repair
was used to resolve that review encoding distinction.

Local verification passed 727 tests, application coverage 90.86%, Ruff lint/format,
mypy on 37 modules and the standard 300-event synthetic smoke. These are software
checks, not progress measured by test count. Remote CI is pending at this local
seal; the PR will record exact remote head, source identity and terminal run.

## Reproduce and interpret

```sh
python -I -B tools/replay_incoming_orders.py \
  --verify-evidence docs/evidence/incoming-order-20261120/run
python -I -B tools/replay_incoming_orders.py --run \
  --plan docs/evidence/incoming-order-20261120/plan.json \
  --output experiments/incoming-order-explicit-reproduction
python -I -B docs/evidence/incoming-order-20261120/independent-review/reference.py \
  --root .
```

Integrity mode verifies hashes/counters without replaying matching. Explicit
reproduction refuses existing outputs; it repeats the same constructed family,
not new evidence. CI uses that explicit fixed-family repeat plus inherited
verification. Original prior records, including PR15's stale progress disclosure,
are preserved; no old optimizer, resource study or PR12 gate is reopened.

This establishes a bounded coherent operator when complete incoming quantities,
activation histories and the stated priority/flow assumptions are supplied.
It does not make those inputs observable in trade-only/L2 data, certify hidden
liquidity or participant behavior, or justify replacing production fills.
Actual research still needs admitted complete order/sequence data, quantity and
residual semantics, venue constraints, latency/queue evidence and defensible
counterfactual response assumptions before any economic evaluation. Existing
exact-state/output-growth/performance and execution-calibration blockers remain.
