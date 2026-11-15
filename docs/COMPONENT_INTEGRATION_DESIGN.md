# Component composition contract, 2026-11-15

Status: preregistration before application imports, numerical probes, source
composition, test collection or engine execution. The source lead and component
mechanics are exposed; these synthetic cases are not market data or holdouts.

## Question and decision

Can the independent PR3–11 fixes form a compatible research-system candidate,
and does the actual engine hand the original exact portfolio P&L to the exact
risk checks despite creating float artifact rows first, with correct boundary
decisions and downstream actions?
This is a bounded integration result, not a new component defect, execution
calibration, performance measurement, economic advantage or merge recommendation.

The primary comparison is C1 (the composition) against C0 (identical source
except main's `risk.py`). C0 deliberately retains a known defect; it attributes
the risk interaction without masking it behind main's earlier accounting errors.
An independent rational ledger derives cash, inventory, fees, marks, net P&L,
high watermark and inclusive loss/drawdown decisions from the original literal
events/costs and fixed expected fill. It must not call production accounting,
marking or risk helpers. Observers delegate original methods once and record the
original Decimal argument, caller context before/after each risk call, decisions,
ordered snapshots, runtime allow-new gates and exchange command/report ordering.
Persisted Float64 rows are checked as projections, never exact Decimal evidence.

## Frozen sources and assembly

Main is `1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f`.
Apply production-only patches in this order, without a merge, three-way fallback
or discretionary conflict resolution:

| PR | Patch from | Patch to |
|---|---|---|
| 3 | main | ceccf0c69a3afbc09b07c993b65614ab09fef838 |
| 4 | main | f5a86c5d18dc9d63634fdea95bbb60db3c5f8eec |
| 5 | main | 22ef318adb1292e6a4f76535a73bae854ed5cca8 |
| 6 | main | c4f3611b650faca290d0946b98620604c5d9cfd6 |
| 7 | main | 79aec058363a04a8ed6b6f6bdbe200d6e9679d13 |
| 8 | main | 1884d4c2e778d00797fbd97fed5b09ba113ed55e |
| 9 | main | 253abee735c02d7805e2a4377c12626742108d84 |
| 10 | PR9 | a105e73cf608c1ff24cdead3dd3c50ee18d8916e |
| 11 | main | 38a17f09746903938fa76b1d42268c8a75c7d3f6 |

The union is 15 production paths. Only metrics.py has an independent overlap:
PR5 duration integration and PR7 quantity/horizon aggregation must both survive.
Keep a full `src/lobmm` byte manifest for each source (exclude build metadata),
patch hashes and original test/input hashes. PR10's PR9 dependency is not applied
twice. Recovery PR1/2 and PR12 measurement tooling are excluded. Prior independent
heads, negative results and stopped November 2/4 candidates remain untouched.

Copy the original added regression tests from PR3–11, PR5's existing metrics-test
adjustment, and only their required frozen fixture/contract JSON files. Preserve
their bytes and dependencies; normal fictional adapter CSVs already on main stay
unchanged. Do not copy historical results or rewrite their source allowlists.
These suites are exposed compatibility checks, separate from the primary trials.

## Fixed inputs and primary six attempts

One hand-authored six-event canonical tape, in original order:

| timestamp ns | sequence | kind | side | price ticks | quantity |
|---|---:|---|---|---:|---:|
| 0 | 0 | SNAPSHOT | BID | 99 | 1 |
| 0 | 1 | SNAPSHOT | ASK | 101 | 1 |
| 10 | 2 | ADD | BID | 100 | 1 |
| 20 | 3 | TRADE | BID | 100 | 1 |
| 30 | 4 | ADD | ASK | 103 | 1 |
| 40 | 5 | ADD | ASK | 103 | 1 |

A fixture subclass uses the existing managed-quote state machine but fixed desired
quotes BID100/ASK102, quantity1. It supplies no exchange/portfolio information.
This isolates wiring rather than evaluates a production trading policy.
Expected own fill is one maker BID at100, quantity1, timestamp20; historical mark
after that trade is100. All subsequent marks remain100. Initial inventory is0.

Common validated config: tick size1; all market/order/report latencies and jitter0,
cancellation latency5; timer interval10, warmup0, mark midpoint, session end MARK;
minimum quote lifetime0, refresh/stale interval1000; order size1, max order1,
max inventory2, max open quantity4, max open orders4, quote age1000, minimum
spread1, no message or volatility limit, no terminal quote cutoff; markout horizons
5/30ns, near-limit fraction0.5; no rebate, taker fee or proportional fee; plots off.
Other AppConfig defaults stay unchanged. Default Decimal context precision28,
ROUND_HALF_EVEN, default exponent/trap settings, with flags cleared at case start.

| Case | literal maker fee per unit | max loss | max drawdown | C1 expectation |
|---|---|---|---|---|
| below_drawdown | 0.99999999999999999999999999999 | 100 | 1 | no kill |
| inclusive_loss | 1 | 1 | 100 | max-loss kill at20 |
| ordinary | 0.01 | 100 | 100 | no kill |

Run each case once per source: C0 then C1 in isolated subprocesses, six primary
engine attempts total. C0 is predicted to falsely kill the below-drawdown case
because its subtraction rounds the exact gap up to1; both sources must kill the
inclusive-loss case and keep the ordinary case enabled. This is a prediction,
not a result. No fee, tape, context, source or criterion selection after outcomes.

Each primary worker has a 120 second wall timeout and the six-attempt stage has
a 900 second cap. Each output destination must not exist; do not overwrite or
automatically retry any failed worker. Infrastructure failures stop this stage.

## Evaluation and stopping

Require exact fill identity and every in-memory net-P&L handoff to match the
independent oracle, including duplicate-timestamp call order; C1 risk decisions
must agree at every call. Preserve caller context for each C1 risk call (not a
whole-engine context claim). Check that the loss kill requests cancellation of
the already accepted remaining ASK, reaches the exchange after the fixed5ns
channel delay, and suppresses new quotes at later engine decisions. Do not demand
exactly one total cancel: the continuous-risk path and strategy suppression may
both send the same ASK cancel; retain both and their terminal responses. Do not demand
no post-kill fills, improved cancellation liveness, rollback across components or
protection from already accepted orders. C0 mismatches must be recorded, not
silently filtered. If the primary predicted differential is absent, report that
negative finding rather than substitute a witness.

Require a post-fill allow-new comparison at 20 (strictly before 40): the common
session cutoff at 40 is not evidence of risk-driven suppression. This clarification
comes from independent static design review before outcomes.

Observation completeness, also fixed by static review before outcomes: require
risk-call timestamps `[0,0,10,10,20,20,20,30,30,40,40]` in order (market marks,
timer snapshots and the fill at 20), linkage to the original snapshot objects,
and six ordered mark/fill projections. Retain an atomic trace prefix on each
observer entry so a killed worker cannot erase all earlier observations; an
unfinished entry is labeled in flight rather than claimed as completed.

Supporting checks on these same artifacts: preserve integer event order through
canonical frame/Parquet roundtrip; derive elapsed inventory exposure over [0,40)
and per-horizon quantity coverage independently. One unit is held over [20,40),
so exposure is20/40, observed horizon5 uses the first market state at30, and
horizon30 is right-censored after session end. Do not infer all nine components
are independently exercised by this tape. Run their original regressions and the
repository full tests, lint, formatting, type check and CI smoke separately.

Budget: one composition, one risk-file ablation, six primary attempts, original
compatibility suites and standard checks. Reproductions in tests/CI are disclosed
as repeats of the same exposed cases, not additional selected trials or holdouts.
At most one justified correctness repair after a fully recorded failure, with
independent approval before rerun; no performance optimization, benchmark, profile,
resampling, additional cohort or tolerance/gate change. Source conflict, oracle
failure, unintended import, missing evidence or unresolved correctness failure
stops the primary study. Harness-only repairs retain every failed attempt and
their provenance; they do not authorize changed economic/mechanical assumptions.

Independent review must challenge this design before execution and audit the
actual source, raw records and artifact projections afterward. Publish only safe
original code/synthetic evidence in a new draft PR. No merge, deploy, trade,
paid service, data acquisition, private/licensed E1 material, security/access
change, denied retry or desktop execution. Performance/output growth and passive
fill calibration remain open limitations even if composition passes.

Preexecution clarification: the independent design review confirmed the fixed
tape/fee/source comparison and requested explicit wall caps/no-overwrite above.
Static inspection also clarified append_snapshot serializes a float row before
observe_pnl receives the original immutable Decimal; exact ingress capture,
rather than chronological risk-before-serialization, is the contract. Neither
clarification changes inputs, source candidate, risk criteria or sample budget.
