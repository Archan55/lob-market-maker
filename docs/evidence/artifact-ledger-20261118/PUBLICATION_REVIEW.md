# Independent publication review, 2026-11-18

**GO for publication of the bounded retrospective result.** No material source,
artifact, arithmetic-evidence or scope blocker was found. This review used saved
bytes and independently reconstructed scalar/hash representations. It did not
execute the application, primary auditor, reference reviewer, tests or resource
workers. The only file authored by this reviewer is this review record.

## Frozen provenance

The pre-execution plan was committed at
`ebb31cd02c97c297d9239a720eda5312c7cb45cf`. The auditor and controls were sealed at
`ba71671`; the separate reference was frozen earlier at `f8fe5f7`.

| Retained object | SHA-256 |
|---|---|
| `plan.json` | `11ca9a5fb462336980b4d785d12894ad3689b8cb397ec9ea00dde3688d326437` |
| `tools/reconcile_artifact_ledger.py` | `5501e5654e7478de9dc6ec1c0eb23abcd399e2d29fa41e0ad7a12da70fdc3f9d` |
| `tests/unit/test_artifact_ledger.py` | `caf70887b14a29af9f5da78d9f015f1ffdde4d0c088aef042b54e84429fdcfc1` |
| `docs/ARTIFACT_LEDGER_DESIGN.md` | `6c2e6d5b630c2e2005a351f185da16b8cbd9b86820fb19072277da806b39493c` |
| `run/manifest.json` | `f23b28389817fc9d314fe2e6f782788da1805316614b18caa8ec8ea8e2d44340` |
| `independent-review/review.py` | `0ebbb02e6b34ffd0f1fc65ff24a88db4ed437f42b2183c1bad09aa69c0d035a5` |
| `independent-review/result.json` | `c54e205cd61cfe09ad4307d4a7dbcb6b9a1ff6a5d35c96aa66713c20ccf7f6d1` |

All 38 current production files match the plan and exact Git blobs at
`0866bf13e449cd66e3862454eaaf424a5172c33a`. All twelve selected file pins, including
the ten input/config/output files and two original worker metadata records,
match their existing PR14 Git blobs at
`ec7b6e0bce491d508c30a44b737950cda503d597`. The inherited resource manifest and
plan hashes match the fixed plan. Current auditor, test and design bytes match
their archived run copies and sealed commit. No post-outcome repair was used.

## Actual artifact checks

The primary manifest lists exactly 18 files, totaling 6,311,421 logical bytes.
Every listed hash and the complete on-disk inventory match. Including its
3,905-byte manifest, the raw run contains 19 files and 6,315,326 bytes. The
controller stdout copy equals the saved report. Both mismatch files and both
worker stdout logs are empty and retained.

Every observed row hash was independently rebuilt from the physical saved PnL
and inventory Parquets, preserving field order, integer/string/null types and
packed Float64 bits. All 22,796 rebuilt hashes equal both stored observed and
expected hashes. Ordered row indices, timestamps, all fill prefixes 0..N, all
806 fill IDs, first-prefix fill timestamps, integer inventory/trade-cash/volume
updates, mark admission and flat-average nullability were checked. All 1,612
saved fill-cost projections agree with literal configured costs and packed bits.

| Cohort | Snapshot rows | Fills | Terminal row / prefix | Mismatches |
|---|---:|---:|---|---:|
| 001 | 2,024 | 26 | 2023 / 26 | 0 |
| 002 | 20,772 | 780 | 20771 / 780 | 0 |

The separate reference source matches its pre-outcome Git copy and result pin.
Its observed whole-row chains were independently rebuilt from the physical
Parquets and equal its expected chains. Counts, input pins, fingerprints,
coverage and repeating-field counts agree with the primary evidence. Its exact
terminal signed acquisition pools divided by signed inventory equal the primary
averages; its exact terminal realized amounts equal the primary closed-PnL sums.
This checks retained reference evidence without executing either implementation.

Both worker records report isolated `-I -B` execution, no application imports
before or after, Python 3.12.14, PyArrow 26.0.0 and PyYAML 6.0.3. The two ordered
attempts completed once with exit zero. Their timestamp differences are
1.020803005 and 9.944940626 seconds; full controller elapsed time is
10.993596328 seconds. Stage, worker, row, fill, rational-size and evidence-size
caps are satisfied. Initial repair index and prior elapsed are zero, with no
prior-evidence link. These are audit cap records, not performance observations.

## Report, coverage and publication limits

The report totals independently reconcile to 569,900 snapshot fields, 91,184
inventory fields and 1,612 fill-cost fields: 662,696 comparisons. There are
434,310 non-null Float64 comparisons and 102,746 repeating snapshot projections.
Actual coverage is 806 maker fills, zero taker fills, zero fees and 806 nonzero
maker rebates; four openings, two flat closes, 393 same-side accumulations,
389 partial closes and 18 reversals. Snapshot coverage is 426 flat, 10,282 long
and 12,088 short rows. Taker, nonzero-fee and proportional-charge execution remain
unexercised by these cohorts.

Cohort 001 retains stale `completed_prefix=24` from a periodic progress update.
The final detailed row is prefix 26, all 26 fill records exist, and the independent
reference consumed all 26 fills. This is a disclosed progress-metadata limitation;
raw source and results remain unchanged. Terminal coverage uses the detailed
records, not that stale field.

All thirteen report links and five local-validation log targets resolve locally.
Saved focused/full-test/smoke summaries support the reported 36 controls,
659 regression passes, 90.86% coverage and 300-event software smoke. The current
CI change distinctly labels saved-record integrity verification and one offline
arithmetic reproduction; inherited tests/smoke remain software checks, and no
resource worker is added. All 32 existing evidence paths match their Git index
blobs, including all seven original `.log` files and the empty logs. This review
record is the one additional file for the publisher to stage.

Only retained original synthetic inputs and derived accounting records are used.
No licensed/private market input or credentials were observed; a bounded scan of
the new evidence found no private-key, AWS-key or GitHub-token patterns. Local
workspace/interpreter paths are ordinary provenance metadata.

The result is conditional on these two exposed, related synthetic cohorts,
emitted fills, admitted recorded midpoint marks and the stated fresh-default
28-digit HALF_EVEN presentation assumption. It does not establish hidden exact
state, historical per-row contexts, causal marks, complete cadence, exact risk
handoffs, realistic fills, economic advantage or resource capacity. This local
review makes no remote-head or CI-success claim. Final commit inclusion,
prior-head preservation and exact-head remote CI must be recorded separately.
