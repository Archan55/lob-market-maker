# Independent result review — 2026-11-18

The independently frozen reviewer completed one pass over cohorts 001 then 002
with zero accounting or projection mismatches. This corroborates the same exposed
records; it is not an additional market trial.

## Frozen source and execution

The reviewer was frozen before artifact comparisons. Its signed acquisition pool
derives realized P&L as trade cash plus the remaining signed pool and unrealized
P&L as inventory times mark minus that pool. It does not store the primary
auditor's acquisition average or sum of closed-position P&L.

The independent presentation reference constructs terminating Decimal values
exactly, then converts to binary64. Repeating values use a separate local Decimal
context with precision 28 and HALF_EVEN before conversion. It imports neither
application code nor primary-auditor helpers and does not change the integer
string guard.

SHA-256 pins:

| File | SHA-256 |
|---|---|
| `review.py` | `0ebbb02e6b34ffd0f1fc65ff24a88db4ed437f42b2183c1bad09aa69c0d035a5` |
| `result.json` | `c54e205cd61cfe09ad4307d4a7dbcb6b9a1ff6a5d35c96aa66713c20ccf7f6d1` |
| `stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

Command, from `/workspace/lob-market-maker-ledger-audit`:

```text
/workspace/lob-market-maker-accounting-fix/.venv/bin/python -I -B /workspace/lob-market-maker-ledger-audit/docs/evidence/artifact-ledger-20261118/independent-review/review.py --root /workspace/lob-market-maker-ledger-audit
```

Stdout and stderr were opened exclusively as `result.json` and `stderr.log` in
this directory. Exit status was **0**; stderr was empty. The result records
`status=complete`, `all_fields_match=true`, and an unchanged integer string guard.
No engine, resource trial, acquisition or application import occurred.

## Independently checked results

The reviewer verified original file hashes, all 38 frozen production files, input
event fingerprints, literal YAML costs, original numeric/schema types, fill clock
and prefix admission, exact ledger identities, and every mandatory snapshot and
inventory projection. Float64 comparisons use packed bits without tolerance.

| Cohort | PnL rows | Fills | Snapshot fields | Inventory fields | Fill-cost fields | Mismatches |
|---|---:|---:|---:|---:|---:|---:|
| 001 | 2,024 | 26 | 50,600 | 8,096 | 52 | 0 |
| 002 | 20,772 | 780 | 519,300 | 83,088 | 1,560 | 0 |

Expected and observed whole-row chain hashes agree within each cohort. Both
terminal signed acquisition pools divided by their signed inventory equal the
primary auditor's exact acquisition averages. Independently derived terminal
realized P&L equals the primary exact closed-P&L sum. Detailed primary row hashes
agree with their observed hashes throughout, and the terminal prefixes are 26
and 780, respectively.

Actual coverage independently agrees with the primary records:

| Cohort | Maker fills | Partial closes | Flat closes | Reversals | Flat / long / short snapshots | Repeating projected fields |
|---|---:|---:|---:|---:|---|---:|
| 001 | 26 | 11 | 1 | 1 | 213 / 1,109 / 702 | 6,998 |
| 002 | 780 | 378 | 1 | 17 | 213 / 9,173 / 11,386 | 95,748 |

Repeating coverage comprises average cost and realized/unrealized P&L in ticks
and currency. All fills are maker fills. Nonzero maker rebates are exercised;
taker fills, nonzero per-unit fees and proportional fees are not exercised.

## Retained metadata discrepancy

The original primary cohort-001 `result.json` retains `completed_prefix=24` from
its last periodic progress update. The primary source updates that field every
256 rows and omits a final update when marking the result complete. This is stale
progress metadata: the detailed fill ledger contains every prefix 1 through 26,
the last row record has prefix 26, and this reviewer consumed all 26 fills while
checking all 2,024 rows. Cohort 002's recorded terminal prefix is 780.

The original source and raw results remain unchanged. No missing financial row
or arithmetic failure was found; the stale progress field must not be presented
as the actual final prefix.

## Limits

The cohorts share an exposed synthetic generator and prefix. Agreement is
conditional on emitted fills, admitted recorded midpoint marks and the declared
fresh-default Decimal presentation assumption. It does not retrospectively prove
hidden exact state, per-row contexts, risk handoffs, causal mark selection,
complete market cadence, fill realism, execution calibration or economic
advantage. No prior optimizer or resource gate is reopened.
