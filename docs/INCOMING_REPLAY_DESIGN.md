# Fixed incoming-order counterfactual replay, 2026-11-20

Preregistered before matcher/reference execution. This is an exact synthetic
mechanics study, not calibrated execution, a production replacement or economics.

## Question and exposure

Can a standalone matcher evolve two separate books through the same completely
specified incoming orders, preserving priority, activation and one quantity
budget, while replacing displaced historical executions rather than adding them?

The supplied Q=11 and Q=8 examples are prior exposed controls. With external bids
6@101, 2@100, 3@99 and own5 behind the six at101, own fills are5 and2. These
examples, their mirrors and the conservation equations are not discoveries.
The additional finite cases below probe distinct chronology, priority, residual
and path-dependence failure modes. All are deliberately constructed with manual
expected outcomes, not untouched observations or a parameter search.

Base is PR16 `2a96738c080b1a62a32e4306721de703b242477a`; all38 production
files remain PR13 C1 `0866bf13e449cd66e3862454eaaf424a5172c33a`. Preserve
main and all prior heads. Do not import or modify production Exchange: its
single-strategy command/self-trade path is not an external-aggressor replay.
No old resource, optimizer, PR12 gate or exposed-cohort study is reopened.

## Synthetic contract

- Prices and quantities are positive integers, lot size1. No monetary outcome,
  fee, rebate, marking, inventory policy or profitability comparison is made.
- One consumed side of a book is modeled. Resting activations are noncrossing by
  construction; no opposite-side book is modeled. Every maker is fully displayed.
  Hidden/reserve liquidity and iceberg replenishment are excluded.
- A complete incoming base quantity is supplied exogenously. Each incoming order
  is an atomic IOC walk; unfilled size terminates and never rests or resurrects.
  No cancellation, interleaving or other response occurs during its walk.
- Only the listed activations and incoming orders occur. Later incoming flow is
  held fixed in both worlds as an explicit synthetic assumption, not a forecast
  of how real participants would respond to changed executions.
- Price priority then displayed activation order applies. The exact synthetic
  `(timestamp_ns, sequence)` total order governs equal timestamps and same-price
  arrival priority. It is not inferred from venue timestamps or the C1 scheduler.
- Aggressor participant differs from every maker. Self-trade prevention, balance/
  funds constraints, auctions, venue price-protection bands and other TIF rules
  are excluded. Only the explicitly supplied hard limit is modeled: sells may
  consume bids at or above it; buys may consume asks at or below it.
- Baseline ignores own activations. Counterfactual includes them. Each has its
  own mutable remaining quantities. Incoming event IDs/size/limits/chronology
  are identical between worlds. Every active maker retains original priority
  after a partial fill.

Official Coinbase documentation is contextual support only. The
[channels page](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels)
describes snapshot/sequence reconstruction and maker/taker identifiers in match
messages. Match capture may be incomplete; received orders need not rest, market
orders can be funds-based, and market done messages omit remaining size.
Inference: grouping matches by taker ID alone does not certify full initial base
quantity, residuals or completeness. A complete order feed is a stronger claim.
[Trading Rules §§1.71–1.72](https://www.coinbase.com/legal/trading_rules) specify
price-display-time; hidden iceberg remainder and refreshed display priority add
rules excluded here. Funds, STP and price protections also matter in real use.
No Coinbase market data or data-use rights were acquired or admitted.

## Finite family and manual oracle

The frozen case file contains18 sell/bid recipes and their buy/ask mirrors:
`price -> 202-price`, including hard limits. Quantities, chronology and expected
order IDs stay identical. Thus36 paired scenarios,72 world paths and40 incoming
events per world (80 matching walks). Mirrors are symmetry controls, not
independent samples. The construction source preserves every manual vector.

Unless stated, external E1=6@101, E2=2@100, E3=3@99 arrive first, own O1=5@101
arrives later, then the incoming sell acts. Tuple lists below are ordered fills;
unlisted makers receive zero. `u` is terminal unfilled incoming quantity.

| Recipe | Incoming / variation | Baseline expected | Counterfactual expected |
|---|---|---|---|
| known11 | Q11, prior exposure | E1:6,E2:2,E3:3;u0 | E1:6,O1:5;u0 |
| known8 | Q8, prior exposure | E1:6,E2:2;u0 | E1:6,O1:2;u0 |
| boundary6 | Q6 | E1:6;u0 | E1:6;u0 |
| mixed12 | Q12 | E1:6,E2:2,E3:3;u1 | E1:6,O1:5,E2:1;u0 |
| exhausted17 | Q17 | E1:6,E2:2,E3:3;u6 | E1:6,O1:5,E2:2,E3:3;u1 |
| better_price | O1@102,Q8 | E1:6,E2:2;u0 | O1:5,E1:3;u0 |
| worse_price | O1@100 behindE2,Q11 | E1:6,E2:2,E3:3;u0 | E1:6,E2:2,O1:3;u0 |
| split_time | E1=4@101, thenO1, thenE4=2@101;Q8 | E1:4,E4:2,E2:2;u0 | E1:4,O1:4;u0 |
| two_own | Oa3 thenOb4@101 afterE1;Q11 | E1:6,E2:2,E3:3;u0 | E1:6,Oa:3,Ob:2;u0 |
| partial_carry | Q8 thenQ5 | firstE1:6,E2:2;u0; secondE3:3;u2 | firstE1:6,O1:2;u0; secondO1:3,E2:2;u0 |
| displaced_carry | Q11 thenQ5 | firstE1:6,E2:2,E3:3;u0; secondnone;u5 | firstE1:6,O1:5;u0; secondE2:2,E3:3;u0 |
| late_activation | Q16 beforeO1 activates | E1:6,E2:2,E3:3;u5 | same;u5; O1 later rests5 |
| eligible_limit | Q11,sell limit100 | E1:6,E2:2;u3 | E1:6,O1:5;u0 |
| excluded_limit | O1@100,Q11,sell limit101 | E1:6;u5 | E1:6;u5 |
| shallow6 | onlyexternalE1=6@101;Q6 | E1:6;u0 | E1:6;u0 |
| shallow11 | same shallow book;Q11 | E1:6;u5 | E1:6,O1:5;u0 |
| tie_before | O1activation precedesQ8 in same-time sequence | E1:6,E2:2;u0 | E1:6,O1:2;u0 |
| tie_after | Q8 precedesO1activation in same-time sequence | E1:6,E2:2;u0 | same;u0; O1 later rests5 |

Expected snapshots/final maker remainders derive only by subtracting these
manual fill vectors from explicit activations; they are not generated by either
matcher. Freeze both case bytes/construction source and all program/control/
design hashes in Git and the execution plan before any family matching.

## Hypotheses, comparator and criteria

Primary: both independent implementations reproduce every manual ordered fill
vector, unfilled quantity and maker remainder in both worlds. At every event:

1. Each incoming: `Q = external + own + unfilled`, all amounts nonnegative.
2. Every activated maker: `entered = cumulative fills + remaining`.
3. No inactive/ineligible fill, priority inversion, quantity reuse, input mutation
   or shared state between worlds; complete event chronology and late activation
   are preserved. Final books retain every activated maker's zero/nonzero balance.
4. Paired incoming identity: `own = (baseline external - CF external) +
   (baseline unfilled - CF unfilled)`. Keep signed differences. External
   displacement can be negative after prior paths diverge.

Baseline without own insertion is the comparator. A separate deliberately
invalid hybrid diagnostic copies baseline external executions and adds CF own
fills with CF unfilled. Its signed budget error is exactly
`baseline external - CF external`. Freeze/check that whole vector, not a claim
that every positive own fill must cause hybrid failure. In displaced_carry the
errors are+5 then-5: terminal totals can hide per-incoming failure. The hybrid
is diagnostic mixing of paired outputs, not a third matching world or a claim
of a production defect. No individual-print cap is proposed.

Predeclared structural witnesses also require: shallow6/shallow11 have identical
baseline executions but CF own0/5; same aggregated top external6 with different
arrival placement gives own2/4 in known8/split_time; same-time sequence order
gives own2/0; later CF flow can consume previously displaced external orders;
late activation cannot consume a terminated IOC residual. These outcomes are
expected controls, not discoveries or real-market estimates.

## Budget, trials, stopping and independent review

One primary pass over the exact36 paired scenarios in frozen order. No seeds,
search, added cases or retuning after outcomes. Maximum64 cases,64 activated
units/case,8 activations and8 incoming orders/case; actual family stays smaller.
Run as one isolated `-I -B` research worker,120-second wall cap and16MiB generated
evidence cap. No application imports or production engine/resource calls.

Record full ordered execution/state traces, every criterion and signed hybrid
error, complete or stopped-prefix status, hashes, runtime and no-app proof.
Refuse existing outputs. Stop after the pass, including on failure; no automatic
retry or post-outcome repair/reanalysis budget. Preserve failure and untested
suffixes. Fixed mathematical/invalid-input controls are software checks, not
extra empirical observations. Freeze and review them before family execution.

Independent design/fixture/source review precedes outcomes. An independently
implemented unit-token reference (one token per displayed unit) runs once on the
same frozen inputs afterward and is independently compared to actual artifacts.
The primary aggregate-order matcher cannot use expected output to choose fills.
CI repeats, if published, are explicit verification of the same constructed
family, not fresh data or additional independent trials. Standard regression
checks remain separate. Report every failure and limitation honestly.

No production-model replacement, real-fill inference, Hyperliquid/data acquisition,
paid service, E1/E2 material, access/security change, desktop, merge, deployment
or trading. Real use still needs admitted complete incoming-order/reconstruction
evidence and defensible counterfactual-response/execution assumptions.
