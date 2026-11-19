# Independent fill-trigger result review — 2026-11-19

The independently frozen reference completed exactly one pass over cohorts 001
then 002, with complete attribution and no invariant failures. All **806** fill
classifications, matched transition IDs, remaining quantities and event-budget
records agree with the primary analyzer. Every numerical summary field agrees;
the implementations retain different explanatory labels for set coverage.

## Source and execution record

The reference was frozen before outcomes. It uses full-event timestamp bisects,
retained per-order transition scans, independent strict external-depth replay,
and an independent `>qqbbqq` event fingerprint. It imports neither `lobmm` nor
primary-analyzer helpers. It ignores appended retrospective cumulative, average
fill-price and queue-position metadata.

| Retained file | SHA-256 |
|---|---|
| `reference.py` | `22b45c4f280a4b74ffe4cc7cc2ae7f270575d6d1bff49c2f3c98a24b7558062d` |
| `result.json` | `56870db9f614ab7e47f7f82a47a785c9be958a375409c31484628c44e73515e1` |
| `stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

Command, from `/workspace/lob-market-maker-fill-provenance`:

```text
/workspace/lob-market-maker-accounting-fix/.venv/bin/python -I -B /workspace/lob-market-maker-fill-provenance/docs/evidence/fill-trigger-20261119/independent-review/reference.py --root /workspace/lob-market-maker-fill-provenance
```

Stdout and stderr were opened exclusively as `result.json` and `stderr.log` in
this directory. Exit status was **0** and stderr was empty. No source or raw-result
changes, retries, application imports, engine/resource runs or acquisitions were
performed. The result retains the complete reference per-fill and per-event
records, their canonical hashes, and the reference summary hashes.

## Actual classifications and shares

| Cohort | Class | Fills / orders / trigger events | Units | Fill-price tick notional | Fill share | Unit share | Tick-notional share |
|---|---|---|---:|---:|---|---|---|
| 001 | Exact price | 11 / 11 / 11 | 95 | 949,192 | 11/26 | 19/49 | 474596/1223991 |
| 001 | Price through | 15 / 15 / 15 | 150 | 1,498,790 | 15/26 | 30/49 | 749395/1223991 |
| 002 | Exact price | 140 / 137 / 140 | 1,120 | 11,166,142 | 7/39 | 7/47 | 5583071/37880836 |
| 002 | Price through | 640 / 640 / 640 | 6,400 | 64,595,530 | 32/39 | 40/47 | 32297765/37880836 |

Cohort 001 totals are 26 fills, 26 filled orders, 26 trigger events, 245 units and
2,447,982 fill-price tick notional. Cohort 002 totals are 780 fills, 777 filled
orders, 780 trigger events, 7,520 units and 75,761,672 tick notional. Exact currency
notionals use the unchanged literal tick value `0.01`; they are retained in the
full summaries. Class order and event sets happen not to overlap in these
records; this is an observed property, not an imposed disjointness assumption.
Order catalogs contain 388 and 3,140 orders, respectively, including unfilled
orders. Coverage ratios use filled-order and associated-event unions rather than
those larger catalogs.

| Cohort | Through distance range | Unit-weighted distance | Through quantity above printed quantity |
|---|---|---|---|
| 001 | 1–4 ticks | 8/5 ticks | 10 associated events |
| 002 | 1–4 ticks | 63/40 ticks | 257 associated events |

All exact-price group quantities remain within their associated printed-trade
budgets. All through fills consume their independently reconstructed full
pre-fill remaining quantity. Through-to-printed ratios across all associated
events range from 0 to 10 in both cohorts. Combined quantity exceeds printed
quantity on the same 10 and 257 events. This is permitted by the documented
through-first model and is an assumption diagnostic, not an exact-budget defect.

## Coverage and agreement checks

The reference verifies both sides: 13 BID and 13 ASK fills in cohort 001; 387 BID
and 393 ASK fills in cohort 002. It independently validates all 1,162 and 9,456
order-transition rows, original immutable identities, clock ordering, status
chains and remaining checkpoints. Three cohort-002 orders have multiple fill
events; cohort 001 has none.

These records do not exercise multiple exact-price own fills on one trigger,
through fills following a prior partial fill of the same order, or a same-time
post-partial-fill cancellation. Those admission paths have mathematical controls
in the primary study; they are not empirical coverage from these cohorts.

The reference's 26/780 per-fill records and 26/780 event records equal the primary
records field-for-field and in retained order. Their parsed canonical hashes were
cross-checked, every summary count and exact-rational share/distance was compared,
and all 15 primary evidence files listed in its manifest were rehashed. No
failure records were present. The frozen 38-file application inventory and
original input hashes/fingerprints were independently checked.

## Limits

The original exchange configuration is `lenient`; the independent strict tape
reconstruction does not change that configuration or claim it was STRICT.
Price-through is enabled, strategy activity is post-only, termination is MARK,
and the original fee/tick settings remain unchanged.

The shares describe reliance on an explicit execution-model assumption in two
overlapping, exposed synthetic cohorts. Exact-price fills also rely on estimated
queues and an exogenous tape. Neither class is calibrated venue execution or a
fill-quality ranking. This review proves no true FIFO priority, missing-fill
detection, pending-cancel history, market response, disabled-rule counterfactual,
profitability or realistic execution calibration. No P&L deletion or residual
profit inference is supported.
