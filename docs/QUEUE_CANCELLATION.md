# Controlled queue-cancellation replay

**SYNTHETIC MECHANICS VALIDATION.** These results test execution and accounting; they do not establish real-market profitability.

The ordinary experiment workflow replays one nine-event canonical CSV through fixed-spread quoting. Only cancellation allocation and latency change across the nine cases. The artifact audit checks the analytical oracle independently of the matching engine, both sides, maker costs, quote lifecycle, causal timestamps, and matching input hashes.

## Tape and analytical oracle

At 0 ms the external bid at 99 ticks and ask at 101 ticks each contain 100 units. The delayed strategy submits one post-only 10-unit quote at each price. Quotes arrive at 0.125, 0.250, or 0.500 ms, before 50 units are added behind each quote at 1 ms. At 2 ms, 60 external units cancel on each side. At 3 ms, a 65-unit trade consumes each resting side. A harmless deeper bid add at 5 ms extends the session so execution reports and positive-latency quote cancellations can drain.

- Back: remove the 50 behind, then 10 ahead; ahead = 90.
- Pro rata: remove 40 of the 100 ahead and 20 of the 50 behind; ahead = 60.
- Front: remove 60 ahead; ahead = 40.

Own fill per side = `min(10, max(0, 65 - ahead))`: **0 / 5 / 10**. The proportional allocation is integral here, so rounding is not a confounder. Trades use resting-side semantics. Price-through fills are disabled; all fills are exact-price maker executions.

## Verified cases

| Latency x | Allocation | Ahead | Fill/side | Quote arrival ns | Gross USD | Fees USD | Rebates USD | Net USD | End inventory |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5 | back_of_queue | 90 | 0 | 125000 | 0.00 | 0 | 0 | 0.00 | 0 |
| 0.5 | pro_rata | 60 | 5 | 125000 | 0.10 | 0.021000 | 0.0050 | 0.084000 | 0 |
| 0.5 | front_of_queue | 40 | 10 | 125000 | 0.20 | 0.042000 | 0.0100 | 0.168000 | 0 |
| 1 | back_of_queue | 90 | 0 | 250000 | 0.00 | 0 | 0 | 0.00 | 0 |
| 1 | pro_rata | 60 | 5 | 250000 | 0.10 | 0.021000 | 0.0050 | 0.084000 | 0 |
| 1 | front_of_queue | 40 | 10 | 250000 | 0.20 | 0.042000 | 0.0100 | 0.168000 | 0 |
| 2 | back_of_queue | 90 | 0 | 500000 | 0.00 | 0 | 0 | 0.00 | 0 |
| 2 | pro_rata | 60 | 5 | 500000 | 0.10 | 0.021000 | 0.0050 | 0.084000 | 0 |
| 2 | front_of_queue | 40 | 10 | 500000 | 0.20 | 0.042000 | 0.0100 | 0.168000 | 0 |

Base latencies (ns): market data 100000, order entry 150000, cancellation 150000, fill report 100000. Every channel has positive delay in every case, with zero jitter. Fill notifications arrive at 3.05, 3.10, or 3.20 ms. Long quote lifetimes avoid refresh confounding; new quotes are suppressed from 3 ms onward.

With tick size 0.01 and equal buy/sell quantity q, trade cash is `(101 - 99) * q` ticks and end inventory is zero. Gross USD = `0.02 * q`; fees = `2 * q * 0.002 + (99 + 101) * q * 0.01 * 0.0001`; rebates = `2 * q * 0.0005`; net = gross - fees + rebates. The audit also checks each fill's cost and realized/unrealized P&L. Pro rata ends with five unfilled units per order cancelled; front ends fully filled; back cancels the unfilled ten units.

## Reproduce and audit

Run from the repository root after installing `.[dev]`:

```bash
python -m lobmm.cli experiment --config configs/queue_cancellation.yaml --name queue-cancellation --latency-multipliers 0.5,1,2
python -m lobmm.cli audit-queue-study --experiment experiments/queue-cancellation --config configs/queue_cancellation.yaml --output docs/QUEUE_CANCELLATION.md
```

- Canonical event-stream SHA-256: `abe7764fa0162b6f444c8223d752dbf4847bafb62536b8950f4f4211b4b36224`
- Fixture CSV SHA-256: `3833652ba3e5781290010be29445b5c0c6762d79adbd212b81ba2e1662439071`
- Base YAML SHA-256: `16d06bd3442f9803cef7f85084b8d19d5761e9945a8ba63a682eaae577d6a04e`

[QUEUE_CANCELLATION.json](QUEUE_CANCELLATION.json) contains the complete deterministic audit, including the stream hash for each case. Each run's summary, diagnostics, validation certificate, and sensitivity row must match that hash. Raw replay tables stay under the ignored experiment directory. Repeated-run tests compare all eight Parquet tables byte for byte and the curated audit; wall-clock performance measurements are excluded.

## Scope and limitations

This witness closes the published demo's inability to separate cancellation policies. The original short synthetic path still produces identical policy metrics. Here latency multipliers are deliberately within the pre-add arrival window, so latency does not change fills. This is a cancellation-policy witness, not latency calibration or an empirical estimate of queue position.

L2 cannot identify cancellation ownership. The external tape remains exogenous, while own fills displace historical external volume in the overlay; both trades happen before strategy feedback. Transition rows' queue estimates reflect final order state, so the ahead values above come from the analytical tape, not historical snapshots in those rows. The nine-event session does not support default 100 ms/1 s/5 s markouts or performance claims. Negative-control tests remove cancellations or move quote arrival after the add and require zero fills under every policy. Those controls are separate from this audited grid.
