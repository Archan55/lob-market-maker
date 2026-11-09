# Positive-price quote construction — November 9, 2026

The source lead is a strategy construction failure at the model's positive-price
boundary. A valid bid 1 / ask 2 book and the default half-spread produce a
rounded bid of zero. Constructing that `DesiredQuote` appears to raise before
the strategy can return its valid ask or handle its managed orders. This is a
static hypothesis pending the frozen experiment below.

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

Results, complete trial accounting, frozen code/test pins, source/artifact
review, reproduction and remote verification will be recorded after the
prescribed experiment. This working report is not a completed result.

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
