# Independent publication review, 2026-11-19

**GO for publication of the bounded source-inferred model diagnostic.** No
material provenance, artifact, numerical-report or scope blocker was found.
This reviewer read saved bytes and independently reconstructed the retained
tape, joins, transition checks and exact summaries. No application, analyzer,
reference program, test suite, engine or resource worker was executed by this
reviewer. The only file authored by this reviewer is this note.

## Frozen provenance and execution controls

The design was preregistered at `9322782` and clarified before outcomes through
`1cebbba`. The analyzer and controls were sealed at `55809e9`; the independently
authored reference was frozen at `0bcb572`. The metadata-only input/source plan
was committed before the primary pass at
`4d26af0389f45b10949b14763c7ac81385a2693d`.

| Object | SHA-256 |
|---|---|
| `plan.json` | `93792cdcc0451fcf404520d25736f2873e79c2df133c6381cd095efaa4178b1c` |
| `tools/audit_fill_triggers.py` | `89a117f26d2df65264d5adc866d81c8042dd167f9c30617573bfb36e1c22393d` |
| `tests/unit/test_fill_triggers.py` | `eb508c64aa26a3792c9e4f55b25a46ca8486e2b09084bbefea1e7826413a5520` |
| `docs/FILL_TRIGGER_DESIGN.md` | `108e928b2e7a87e038e547e6f9665b45fb37222c8cf762ce1c6840135b79467d` |
| `run/manifest.json` | `526bda7f603d898feaa8735c36e91bf6e887fa32ac687877ef76a2e35002aa9f` |
| `independent-review/reference.py` | `22b45c4f280a4b74ffe4cc7cc2ae7f270575d6d1bff49c2f3c98a24b7558062d` |
| `independent-review/result.json` | `56870db9f614ab7e47f7f82a47a785c9be958a375409c31484628c44e73515e1` |

Before outcomes, all ten selected input/orders/fills/config/result files were
independently checked against original PR14 manifest hashes, logical byte sizes
and exact Git blobs at `ec7b6e0bce491d508c30a44b737950cda503d597`. The original
resource manifest and plan retain SHA-256 `a0022001...` and `ffcfa3ba...`,
respectively; their full hashes are fixed in the study plan. All 38 production
files match the C1 inventory, original resource plan, PR13 Git blobs at
`0866bf13e449cd66e3862454eaaf424a5172c33a` and PR15 base `95dbd73`.

Static review checked exact cohort/path/source selection, archived analyzer/test/
design identity, sequential isolated `-I -B` workers, first-operation controller
timing, worker timeouts, durable failures and processed prefixes, explicit
untested suffixes, output-cap enforcement and absence of retry/repair paths.
The fixed 100k input rows, 100k transitions, 20k fills, 120-second workers,
300-second controller and 64 MiB evidence limits match the committed plan.
Metadata-plan creation reads hashes/metadata only; saved-evidence verification
checks integrity and counters without reclassifying the original records.

## Physical artifacts and independent reconstruction

The complete original run contains 15 inventoried files plus its manifest:
16 files and 513,838 logical bytes. Every inventory hash and every on-disk path
match. Both failure files and worker stdout logs are empty and retained. Current
source/analyzer/test/design bytes remain unchanged from the sealed source; all
archived harness copies and the original/current plan agree with their pins.

Both attempts completed once in order 001 then 002 with exit zero, full prefixes
and no untested suffix. Recorded worker lifetimes are 0.421574209 and 2.574442610
seconds; full controller elapsed is 3.012382005 seconds. Timestamp differences,
ordering and all caps check out. Runtime records identify Python 3.12.14,
PyArrow 26.0.0 and PyYAML 6.0.3, with no application imports before or after.
These are audit-budget records, not resource or throughput observations.

The reviewer independently decoded all six selected original Parquets, checked
their required Arrow types, and reconstructed all 22,000 external tape rows with
integer depth dictionaries. Ordering, strict no-overdraw/no-crossing admission
and both original event fingerprints agree. Every one of 10,618 registry
transition rows was checked for immutable identity, clocks, lifecycle/status and
remaining-quantity consistency, including unfilled orders.

All 806 fills were independently joined to their immutable order and exactly one
canonical event at the fill timestamp. Every trigger is a matching-side TRADE;
own limits, prices and clocks agree. Remaining quantity and the one-for-one fill
transition checkpoints were reconstructed independently. No appended final
cumulative/average/queue metadata was used as historical state. Every saved
per-fill record and every associated-event budget record matches the independent
reconstruction, including exact-price budget flags and full-remainder through
quantities.

## Exact results and coverage

| Cohort | Exact fills / units / tick notional | Through fills / units / tick notional | Through unit share | Through notional share |
|---|---|---|---|---|
| 001 | 11 / 95 / 949,192 | 15 / 150 / 1,498,790 | 30/49 | 749395/1223991 |
| 002 | 140 / 1,120 / 11,166,142 | 640 / 6,400 / 64,595,530 | 40/47 | 32297765/37880836 |

Every exact-rational class share, class/union order and event count, literal-tick
currency notional and distance statistic was rebuilt and matches the saved
summary. Display percentages in the report round correctly. Through distances
are 1–4 ticks, with unit-weighted means 8/5 and 63/40 ticks. Class order/event
overlaps are zero here. Catalogs contain 388 and 3,140 orders. The latter cohort
has three orders with repeated exact-price fills. Both sides are represented.

| Cohort and event denominator | Through units below print | Equal to print | Above print | Total |
|---|---:|---:|---:|---:|
| 001, through events only | 5 | 0 | 10 | 15 |
| 001, all associated events | 16 | 0 | 10 | 26 |
| 002, through events only | 376 | 7 | 257 | 640 |
| 002, all associated events | 516 | 7 | 257 | 780 |

All printed denominators are positive. Through-only ratio ranges are 5/52 to 10
and 5/103 to 10. Exact-only events contribute zero through/print ratios. Combined
own quantity exceeds print on 10 and 257 events; the documented through rule
permits this. It is an assumption diagnostic, not an exact-price budget defect.
No exact-price budget failed. Each actual associated event has one own fill;
shared multi-own-fill budgets, partial-exact then through on one order, and
same-time post-partial cancellations remain hand-control coverage only.

The retained reference source matches its pre-outcome Git copy and stated hash;
its result hash and empty stderr match the review pins. All 806 reference fill
records and 806 event records equal the primary records in retained order.
All common mathematical summary fields agree, and every reference canonical
record/summary hash was independently recomputed. Explanatory label names differ
as disclosed. Reference input pins, fingerprints and repeated-fill/side coverage
also agree with the physical records. This is corroboration of the same exposed
records, not another market sample.

## Publication, CI and limits

All 32 existing evidence files match their Git index blobs, including all ten
original `.log` files and empty logs. No bytecode/cache file is included. This
note is the one additional path for the publisher to stage. The full original
run is retained; selected inputs remain in the existing committed PR14 bundle.
After this note is written, all sixteen report links resolve locally. Every
local-validation log target exists. Saved logs support the reported 31 controls,
690 regression passes, 90.86% coverage, Ruff lint/format, mypy on 37 modules and
300-event software smoke; the local source proof identifies this worktree and
all 38 C1 hashes.

The CI change clearly separates saved-record integrity checks from one explicit
fixed-input attribution repeat and retains original plus CI evidence. Inherited
PR14 saved-resource verification and PR15 offline reconciliation remain disclosed
verification steps. None is a new market observation or resource worker trial.
Remote exact-head CI is explicitly pending at this local seal; this review makes
no independent remote-head or remote-success claim.

No licensed/private market data or credentials were observed in the new bundle.
A bounded scan found no private-key, AWS-key or GitHub-token patterns. Inputs are
the unchanged original synthetic records; workspace/interpreter paths are normal
provenance metadata. Only this review note was edited by this reviewer.

Attribution is source-inferred: no original branch label or disabled-rule
counterfactual was retained or generated. Independent strict external tape proof
does not relabel the original exchange's `lenient` mode. These overlapping,
exposed synthetic cohorts establish neither actual FIFO/queue priority, missing
fills, complete command/report causality, calibrated execution nor economics.
Exact-price fills are also model-dependent. Do not delete through fills from P&L,
infer residual profit, retune costs or infer a disabled-rule strategy outcome.
Prior studies, engineering gates and execution-calibration prerequisites remain
open. Final commit inclusion, prior-head preservation and remote CI are separate
publication checks for the parent task.
