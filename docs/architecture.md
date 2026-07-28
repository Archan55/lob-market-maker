# Architecture

## Event flow

```mermaid
flowchart LR
    H["Historical exchange event"] --> T["True exchange book"]
    T --> C["Delayed market-data channel"]
    C --> O["Strategy-observed book"]
    O --> S["Strategy decision"]
    S --> E["Delayed order or cancel"]
    E --> X["Simulated exchange"]
    X --> Q["Shared queue and fill logic"]
    Q --> P["Portfolio and risk"]
    P --> M["Metrics and report"]
    X --> R["Delayed execution report"]
    R --> S
```

The backtest orchestrator wires components but does not expose exchange-owned
objects to strategies.

## Ownership boundaries

| Boundary | Owns | May expose |
|---|---|---|
| Data layer | Canonical immutable events and schemas | Ordered event iterator |
| Scheduler | Clock, heap, causal waves, monotonic schedule IDs | Event payloads to registered handlers |
| Exchange | True L2 book, live orders, queue overlay, transitions | Immutable reports and audit rows |
| Strategy runtime | Observed L2 book, known account, shadow orders | Frozen strategy context |
| Portfolio | True cash, inventory, average cost, costs, marks | Immutable accounting snapshots |
| Risk | Limits, kill-switch state, rejection audit | Decisions and cancel requests |
| Strategy | Its signal history and quote intent | Actions only |
| Evaluation | Completed audit, true future marks | Post-run metrics and artifacts |

`metrics` and markout functions consume completed results. They are not
dependencies of strategy modules.

## Deterministic scheduler

Every item is ordered by:

```text
(timestamp_ns, causal_wave, phase, source_sequence, schedule_id)
```

```mermaid
sequenceDiagram
    participant Feed as Historical feed
    participant Ex as Exchange
    participant MD as MD channel
    participant Rt as Strategy runtime
    participant Cmd as Command channel

    Feed->>Ex: market event (phase 10)
    Ex->>MD: immutable delayed event
    MD->>Rt: delivery (phase 30)
    Rt->>Rt: coalesced decision (phase 40)
    Rt->>Cmd: action
    Note over Cmd,Ex: zero-latency action moves to next causal wave
    Cmd->>Ex: arrival (phase 20, next wave)
```

Phases are stable integers, source sequence preserves channel order, and a
monotonic schedule ID breaks every remaining tie. Scheduling at the current
time into a phase that has already started increments the wave. A finite wave
guard detects immediate-response loops.

## Book and queue separation

The true book is the historical aggregated external book. Simulated resting
orders are not inserted into its price dictionaries; they live in a queue
overlay keyed by `(side, price_ticks)`.

```mermaid
flowchart TD
    L["Price-level queue"] --> A["External segment: ahead"]
    A --> B["Own order A"]
    B --> C["External add: behind A"]
    C --> D["Own order B"]
```

An exact-price trade supplies one mutable volume budget and walks this sequence
once. Cancellation models reduce external nodes only. Queue-ahead and
behind-volume are derived by scanning nodes around the requested order, which
also handles multiple own orders without reusing trade quantity.

The external book and overlay are re-anchored on reset/snapshot. Their
counterfactual limitations are documented and surfaced in diagnostics.

## Data and command channels

A latency model returns a deterministic nonnegative delay from its own seeded
generator. Each channel stores its last delivery timestamp and clamps the next
delivery time to avoid reordering messages. Payloads are frozen values:

- market channel: original `MarketEvent`;
- command channel: immutable new/cancel request plus decision/send metadata;
- report channel: immutable acknowledgement, rejection, cancellation, expiry,
  or fill report.

A delayed market message never carries a reference to the mutable true book.

## Order lifecycle

```mermaid
stateDiagram-v2
    [*] --> PENDING_ARRIVAL
    PENDING_ARRIVAL --> REJECTED
    PENDING_ARRIVAL --> LIVE
    LIVE --> PARTIALLY_FILLED
    LIVE --> FILLED
    LIVE --> CANCELLED
    LIVE --> EXPIRED
    PARTIALLY_FILLED --> PARTIALLY_FILLED
    PARTIALLY_FILLED --> FILLED
    PARTIALLY_FILLED --> CANCELLED
    PARTIALLY_FILLED --> EXPIRED
    REJECTED --> [*]
    FILLED --> [*]
    CANCELLED --> [*]
    EXPIRED --> [*]
```

The exchange is authoritative. Strategy `pending_cancel` state does not alter
exchange fill eligibility. Every transition and fill has a unique deterministic
ID and is appended to the audit trail.

## Accounting path

At exchange fill time:

1. order remaining quantity and cumulative notional update;
2. fee/rebate economics are calculated from liquidity role;
3. portfolio books signed inventory and trade cash;
4. realized average-cost P&L updates;
5. gross/net mark identities are checked;
6. an immutable fill report is delayed to the strategy.

The strategy-known account changes only at step 6.

## Package dependency direction

```text
enums/types/events/config
        ↓
data ─ book ─ orders ─ scheduler/channels
        ↓        ↓             ↓
     queue_model ───────── exchange
                         ↓
                 portfolio/risk
                         ↓
              strategy_runtime/strategies
                         ↓
                     backtest
                         ↓
               metrics/report/cli
```

Lower layers never import strategy, reporting, or CLI modules. Core logic does
not depend on notebooks.

## Failure behavior and audit

- Invalid configuration or canonical data fails before replay.
- Strict book errors are descriptive; lenient clamping increments named
  counters and logs context.
- Risk rejections, stale cancels, post-only rejects, malformed historical
  reductions, reset behavior, and missing future marks are auditable.
- CLI commands convert failures to helpful messages and nonzero exit codes.
- Generated outputs are written beneath the configured run directory; large
  run artifacts and raw data are ignored by version control.
