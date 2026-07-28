# Résumé guidance

Use measured, reproducible facts only. Never replace brackets with synthetic
profitability claims or invented throughput.

Template:

> **Event-Driven Market-Making Simulator | Python, NumPy, Polars**
>
> - Built a Level 2 order-book simulator processing **[MEASURED NUMBER]**
>   market events per second on **[DATASET SIZE AND HARDWARE]**, modelling
>   queue position, partial fills, fees, rebates, and configurable
>   order-entry/market-data latency.
> - Developed an inventory- and order-flow-aware quoting strategy using
>   microprice and book imbalance, improving **[REAL MEASURED METRIC]** by
>   **[REAL MEASURED VALUE]** relative to a fixed-spread baseline after
>   transaction costs on **[APPROPRIATE HISTORICAL OUT-OF-SAMPLE DATA]**.

The second bullet must not be populated from the provided synthetic
demonstration. If no suitable real evaluation exists, use a mechanics-only
version:

> - Implemented fixed-spread, inventory-aware, and microprice quoting behind a
>   no-look-ahead strategy boundary, with deterministic latency, queue
>   sensitivity, risk limits, markouts, and accounting-identity tests.

A truthful engineering-only bullet from the recorded release benchmark is:

> - Built a deterministic Level 2 market-making simulator that replayed 20,000
>   synthetic market events at a median 4.9k events/second (4.2k lower-tail)
>   across five full-loop runs on an Intel i7-1255U under an active desktop
>   workload, with causal latency channels, queue-aware partial fills,
>   continuous risk controls, and hashed audit artifacts.

Keep “synthetic,” “median,” the event count, repeat count, timing scope, and
hardware when using that number. Re-run it if the code or machine changes.

Before quoting throughput:

1. use `benchmark-full` for whole-simulator claims and `benchmark` only for
   reference-book reconstruction;
2. preserve the exact command, commit, Python version, CPU, dataset
   provenance, event count, and event-stream hash;
3. report the number of repeats and a stable statistic such as median
   throughput plus a lower-tail value;
4. state what the timer excludes, especially metrics, serialization, and
   plots.

Before quoting strategy improvement:

1. use legally obtained, appropriate historical data;
2. specify baseline, period, universe/instrument, and split;
3. include realistic costs and sensitivity to latency/queue assumptions;
4. verify the metric is out of sample and statistically interpretable;
5. retain generated artifacts so the claim is reproducible.
