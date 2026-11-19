# Price-through reliance in retained synthetic maker fills

In the fixed 20k-event cohort, **85.11% of emitted maker units and 85.26% of
execution notional are associated with the documented price-through rule**.
The 2k cohort's corresponding shares are 61.22% and 61.23%. All 806 emitted fills
were uniquely attributed with zero consistency or exact-price budget failures.
An independently authored reference and a separate physical-artifact review
corroborated every classification and event record.

This quantifies reliance on a modeling rule, not actual queue execution quality.
The normal synthetic outputs cannot serve as passive-fill calibration. No
production defect, strategy improvement or economic advantage was established.
No production source changed.

## Frozen question, sources and exposure

[The design](FILL_TRIGGER_DESIGN.md) was committed at `9322782`, with pre-outcome
clarifications through `1cebbba`. Two independent source/design reviewers
approved attribution as identifiable under its strict admission rules. The
analyzer and 31 fixed hand controls were sealed at `55809e9`; the separate
reference was frozen earlier at `0bcb572`. The [input/source plan](evidence/fill-trigger-20261119/plan.json)
was committed at `4d26af0389f45b10949b14763c7ac81385a2693d` before the first join.
Its SHA-256 is `93792cdcc0451fcf404520d25736f2873e79c2df133c6381cd095efaa4178b1c`.

Base is PR15 `95dbd73d75ffc95b127d9c21e825e940ce230d0f`; the original artifacts
remain PR14 `ec7b6e0bce491d508c30a44b737950cda503d597`. All 38 production files
are still byte-identical to PR13 C1 `0866bf13e449cd66e3862454eaaf424a5172c33a`.
The plan binds each cohort's original input, orders, fills, literal config and
worker result, original manifest/plan identities and event fingerprints, and all
analyzer/test/design hashes. [Prerequisite review](evidence/fill-trigger-20261119/prerequisites.json)
records all fifteen unchanged draft heads, empty comments/issues and successful
existing head-associated CI runs before new classifications.

The selected cohorts are the original PR14 runs 001 (2,000 events) and 002
(20,000 events). They are exposed seed-7 synthetic records with overlapping
generator/prefix history, not independent market sessions or fresh holdouts.
Prior PR15 accounting results and 26/780 maker-fill counts were known; the new
fill-to-trade classifications were not. Runs 003/004 were excluded before
outcomes as previous repeats. No additional seed, window, configuration, source
or favorable subset was tried.

PR1's controlled cancellation witness disabled price-through; PR15 reconciled
accounting conditional on supplied fills. This study asks a different question
about the normal outputs' trigger provenance. An unrelated static Tardis
timestamp-group lead lacked an atomic-message contract and was not probed.

## Method and checks

Under the frozen source, maker fills arise only from queue-update intents.
Exact-price intents and strictly better-price through intents are disjoint for
an order's fixed limit. The original trigger string is not persisted, so the
classification is **source-inferred**, not a recovered recorded label.

For every fill, admission required exactly one canonical EVENT at that timestamp,
then a matching-side TRADE and equal or strictly better own limit. It did not
prefilter trades and guess among other same-time events. Immutable order IDs,
side, limit, original size and clocks were joined consistently. Remaining size
was reconstructed from original size minus earlier fills and checked against
every immutable lifecycle transition. The appended queue estimates and final
cumulative/average fields were deliberately ignored as historical state.

An independent integer depth replay checked all 22,000 original tape rows without
overdraw, crossing or clamping and reproduced both event fingerprints. Original
exchange validation mode remains `lenient`; original data mode is `strict`.
The no-clamping result comes from the independent replay and all-maker,
post-only/MARK path, not from relabeling the exchange configuration.

Through fills must consume the order's entire pre-fill remaining size. Exact
fills must fit its remaining size, and all exact own fills associated with one
trade must sum to no more than that trade's raw/applied printed quantity.
Through quantity is intentionally outside that same print budget in this model.
These are necessary consistency checks; they do not independently reconstruct
FIFO priority or determine whether missing fills should have occurred.

The literal tick is `0.01` USD; maker fee `0`, rebate `0.0002` per unit, taker
fee `0.0003`, proportional rate `0`. Settings were unchanged, not calibrated or
tuned. Notional is unsigned quantity times fill-price ticks, with exact literal
tick conversion for currency. Shares are exact rational values in the evidence;
percentages below are display rounding. No net-P&L attribution was calculated.

## Exhaustive observed attribution

| Cohort | Exact fills | Exact units | Through fills | Through units | Through unit share | Through notional share |
|---|---:|---:|---:|---:|---:|---:|
| 001: 2k events | 11 | 95 | 15 | 150 | 61.22% (`30/49`) | 61.23% |
| 002: 20k events | 140 | 1,120 | 640 | 6,400 | 85.11% (`40/47`) | 85.26% |

Totals are 26 fills/245 units and 780 fills/7,520 units respectively. Tick
notionals are 2,447,982 and 75,761,672; through portions are 1,498,790 and
64,595,530. Exact through-notional shares are `749395/1223991` and
`32297765/37880836`. These are simulated execution notionals, not profits.

The 001 classes touch 11 exact and 15 through orders/events. In 002, 140 exact
fills touch 137 orders and 140 events; 640 through fills touch 640 orders/events.
Three exact-price orders have repeated fills. No class order or event sets
overlap in these records, though the protocol allows overlaps. Catalogs contain
388/3,140 orders and 1,162/9,456 transition rows. All 10,618 transitions and 806
fill checkpoints were checked, including non-filled order lifecycles.

Through distances are 1–4 ticks in both cohorts; unit-weighted means are
`8/5 = 1.6` and `63/40 = 1.575` ticks. Distance is relative to the associated
worse-price print, not a markout, venue slippage estimate or economic edge.

| Through-trigger events only | Own through units below print | Equal to print | Above print | Total | Through/print range |
|---|---:|---:|---:|---:|---|
| 001 | 5 | 0 | 10 | 15 | `5/52` to `10` |
| 002 | 376 | 7 | 257 | 640 | `5/103` to `10` |

All exact-price budgets passed. Through-plus-exact totals exceed printed
quantity on 10/257 associated events, with a maximum through/print ratio of
10 in both cohorts. This is the declared full-remainder through assumption,
**not an overspending defect**. Including exact-only events adds 11/140
zero-through ratios to the below-print column; those denominators must not be
confused with through-trigger events only.

Every actual associated event has one own fill. Thus multiple-own-fill shared
budget pressure, partial-exact followed by through on the same order, and
same-time post-partial cancellation are hand-control coverage only. The data
do exercise both sides and repeated exact fills. No taker execution is covered.

## Trial ledger, review and verification

One preregistered primary pass ran 001 then 002. Both completed, with zero
failures, ambiguous joins, untested suffixes, retries, post-outcome repairs,
changed criteria or added cohorts. Static review corrected an analyzer lifecycle
validation gap before any numerical controls or actual joins. All 31 fixed hand
controls then passed before the primary pass. They validate emitted-record
invariants, not full FIFO-feasible synthetic replay or calibrated execution.

The primary controller recorded 3.012382005 seconds and workers 0.421574209 /
2.574442610 seconds, within the frozen 120/300-second caps. These are budget
records, not throughput measurements. Its 15 inventoried files plus manifest
total 16 files/513,838 bytes, below 64 MiB. No engine or resource worker was
invoked for classification, and no application module was imported.

One independently authored reference pass used full-event timestamp bisects and
per-order transition scans, without the primary analyzer/application helpers.
All 806 per-fill and 806 per-event records and nested mathematical summaries
agree semantically; explanatory text labels are not claimed byte-identical.
A third reviewer independently rebuilt the physical joins, checkpoints, class
totals, budget flags and manifest hashes from the original Parquets. These are
independent assistant reviews, not external human review or extra market samples.

Local regression checks passed: 690 tests, application coverage 90.86%, Ruff
lint/format, mypy on 37 modules and the standard 300-event synthetic CLI smoke.
They are separate software validation, not research progress measured by test
count. [Local validation](evidence/fill-trigger-20261119/local-validation.json)
distinguishes those checks from the unrun remote stage. Remote CI is pending
at this local seal; the PR will carry the final remote commit and terminal run.
The workflow explicitly repeats this fixed attribution as verification, alongside
inherited PR14 saved-resource verification and PR15 offline ledger reconciliation
on their unchanged exposed inputs. Those CI repeats do not alter the original
records or become new study observations.

## Reproduction and full evidence

From this branch root with Python 3.12 dependencies:

```sh
python -I -B -m pytest tests/unit/test_fill_triggers.py -q
python -I -B tools/audit_fill_triggers.py \
  --verify-evidence docs/evidence/fill-trigger-20261119/run
python -I -B tools/audit_fill_triggers.py --run \
  --plan docs/evidence/fill-trigger-20261119/plan.json \
  --output experiments/fill-trigger-explicit-reproduction
python -I -B docs/evidence/fill-trigger-20261119/independent-review/reference.py \
  --root .
```

Verification checks immutable hashes/counters only; it hides no classification
rerun. An explicit reproduction refuses an existing output directory and is a
repeat of the same exposed records, not a new trial or holdout.

- [Complete original manifest](evidence/fill-trigger-20261119/run/manifest.json)
  and [primary report](evidence/fill-trigger-20261119/run/report.json).
- Original [001 fill records](evidence/fill-trigger-20261119/run/cohorts/001/fill.jsonl),
  [001 event budgets](evidence/fill-trigger-20261119/run/cohorts/001/event.jsonl),
  [002 fill records](evidence/fill-trigger-20261119/run/cohorts/002/fill.jsonl) and
  [002 event budgets](evidence/fill-trigger-20261119/run/cohorts/002/event.jsonl).
  The manifest also inventories both empty failure logs, runtime/import checks,
  exact frozen analyzer/controls/design and the selected plan.
- [Frozen independent source](evidence/fill-trigger-20261119/independent-review/reference.py),
  [result](evidence/fill-trigger-20261119/independent-review/result.json) and
  [review](evidence/fill-trigger-20261119/independent-review/REVIEW.md).
  Source/result SHA-256 pins are `22b45c4f280a4b74ffe4cc7cc2ae7f270575d6d1bff49c2f3c98a24b7558062d`
  / `56870db9f614ab7e47f7f82a47a785c9be958a375409c31484628c44e73515e1`.
- [Physical artifact/publication review](evidence/fill-trigger-20261119/PUBLICATION_REVIEW.md),
  [saved-record integrity receipt](evidence/fill-trigger-20261119/integrity-verification.json)
  and [local source proof](evidence/fill-trigger-20261119/local-source-proof.json).

## Practical decision and remaining gaps

The large through share makes the normal cohort's fill volume unsuitable as
evidence that estimated exact-price queue behavior is realistic. Exact-price
fills remain model-dependent too. Do not remove through fills from P&L, infer
the result of disabling the rule, or promise a more profitable strategy: changed
fills would alter later inventory, orders, queue state and decisions.

This resolves the previously unquantified trigger composition for these fixed
artifacts. It does not resolve passive-execution calibration, missing-fill/FIFO
truth, complete command/report causality or counterfactual market response.
Realistic evaluation still needs admitted synchronized L2/trade data with
reconstruction/gap evidence, venue priority/trade semantics, fee rules and
defensible feed/order/cancel/report latency and queue/fill calibration.

PR3 performance/exact-state/output growth, stopped November 2/4 candidates and
PR12's indeterminate gate remain unchanged. No optimization follows. Main and
all earlier draft heads are preserved; PR15's original stale progress-field
disclosure is not altered. Review this as a bounded model diagnostic while those
economic-data and engineering prerequisites remain open.
