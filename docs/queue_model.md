# Level 2 queue model

## Why an approximation is necessary

Aggregated Level 2 data reports total quantity at a price. It does not identify
individual orders, exact priority, hidden/reserve size, or which participant
cancelled. Exact queue position is therefore not observable.

The model provides a deterministic, inspectable approximation for sensitivity
analysis. It must not be described as reconstruction of an actual venue queue.

## Shared segment ledger

Every active `(side, price_ticks)` has one FIFO list containing:

- external segments, with stable segment IDs and estimated quantities;
- own segments, each tied to one exchange order.

When the first own order rests at a price containing 100 displayed units:

```text
[external 100] [own A 10]
```

If 20 units are subsequently added and own order B then arrives:

```text
[external 100] [own A 10] [external 20] [own B 5]
```

For A, external ahead is 100 and external behind is 20. For B, queue ahead
includes 100 external, A's remaining 10, and the later external 20.

## Trades and partial fills

An exact-price trade walks the list once with one mutable quantity budget.
External segments consume budget without generating fills. Own segments reached
by the remaining budget emit partial/full fill intents.

In the example, a trade of 108 removes 100 external then fills 8 of A. It cannot
also reuse 108 against B. A subsequent trade sees A's remaining 2 before the
later external segment and B.

An own order never fills from a trade whose exchange timestamp precedes its
arrival because its segment did not exist then. A cancellation sent by the
strategy does not remove its segment until exchange arrival.

## Historical cancellations

Only external segments are eligible. Given external segments on both sides of
an own order:

- **back_of_queue** visits from the tail. It removes estimated volume behind us
  first and is the conservative baseline.
- **front_of_queue** visits from the head. It removes volume ahead first and is
  optimistic.
- **pro_rata** assigns integer removal proportional to external segment size,
  then distributes the rounding remainder by largest fractional remainder with
  stable segment-ID ties.

None is an observable fact. Results should report/configure the choice and
include sensitivity runs for material conclusions.

### Integer pro-rata rounding sensitivity

Integer pro-rata allocation also has rounding sensitivity: increasing the
cancellation quantity need not increase the volume removed ahead of an own
order. For the same initial queue
`[external 1] [own 1] [external 5] [external 3]`, independent cancellation
scenarios give:

| Cancel quantity | External removals, in segment order | External remaining | Own fill from next trade of 1 |
| --- | --- | --- | --- |
| 4 | 1, 2, 1 | 0, 3, 2 | 1 |
| 5 | 0, 3, 2 | 1, 2, 1 | 0 |

These are deterministic largest-remainder results, with total cancellation
conserved and the own segment untouched by cancellation. The larger
cancellation leaves one external unit ahead, which consumes the next trade's
budget. This is expected model sensitivity, not evidence of an allocation bug
or actual venue behavior. A monotonic relationship between cancellation size
and own fills must not be assumed for this integer approximation.

## Adds

Displayed adds after our order append behind it. This assumes ordinary
price-time priority. A venue with size priority, pro-rata matching, pegged
orders, or special participant priority requires a different adapter/model.

## Price-through fills

If a trade occurs at a worse price than a resting own order on the consumed
side, baseline logic assumes the better own price was swept and fills its
eligible remaining quantity. Exact-price fills remain constrained by the
reported trade budget.

This rule is configurable because feeds can aggregate or omit events in
venue-specific ways.

## External-book reconciliation

The historical book is external displayed truth. The overlay injects
counterfactual own liquidity but does not cause subsequent historical agents to
change behavior. Once our segment receives a simulated fill, overlay external
quantity may differ from the recorded book path.

The simulator diagnoses this divergence. It does not silently invent a market
response. Reset/snapshot discards queue priority and, by conservative default,
expires affected own orders.

## Manually auditable example

1. Bid 100 has 10 external units; ask 102 has 10.
2. Own bid for 5 arrives at 100: queue ahead = 10.
3. Five bid units cancel with no estimated behind volume: queue ahead = 5.
4. Ten bid units add after us: behind = 10.
5. Trade 8 at bid 100: five external ahead are consumed, own order fills 3.
6. Trade 2 at bid 100: remaining own 2 fills.

The matching test then books a five-unit buy at 100. Marked at 101 with tick
size 0.01, gross P&L is `5 * (101 - 100) * 0.01 = 0.05`, before separately
booked costs/rebates.

## Validation properties

Tests assert:

- queue-ahead and behind estimates never become negative;
- one trade budget is not duplicated across own orders;
- fill quantity never exceeds order remaining quantity;
- cancellation removes only external segments;
- removing an earlier own order updates later order priority;
- cancels before and after a potential trade produce the documented result;
- no fill occurs before venue arrival;
- deterministic input and assumptions produce deterministic output.
