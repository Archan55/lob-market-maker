# Independent unit-token result review, 2026-11-20

**GO for the bounded synthetic mechanics result.** The independently authored
unit-token reference matches all 36 frozen scenarios, 72 world paths, 80 incoming
walks and 360 event-state snapshots. All 40 paired incoming records match the
primary results semantically. No matching, priority, state, conservation or
manual-oracle discrepancy was found.

## Independence, source and execution

The reference uses one immutable token per displayed unit, orders eligible
tokens by price and original activation `(timestamp_ns, sequence)`, selects the
first supplied incoming quantity and removes only those tokens. It coalesces
selected units into ordered maker fills afterward. This differs from the
primary aggregate-order matcher and imports neither that matcher nor `lobmm`.
Only event inputs and resting side reach the matching operator; the expected
subtree is accessed after both actual worlds have been derived.

The parent approved the full design and manual fixture before reference
authoring. Ruff formatting/lint and syntax compilation were the only pre-GO
checks; no numerical case, matching call, source import or unit test was run
before the explicit execution GO. The source was frozen and preserved in the
repository before primary outcomes. This reference executed exactly once, with
no source edit, retry, repair, extra case or substituted input afterward.

Exact command (fresh output paths, shell `noclobber` enabled):

```sh
/workspace/lob-market-maker-accounting-fix/.venv/bin/python -I -B \
  docs/evidence/incoming-order-20261120/independent-review/reference.py \
  --root /workspace/lob-market-maker-incoming-replay \
  > docs/evidence/incoming-order-20261120/independent-review/result.json \
  2> docs/evidence/incoming-order-20261120/independent-review/stderr.log
```

Exit status was 0. Standard error is empty. The result is 98,408 bytes and
records 41,127,109 ns elapsed, within its 120-second/16-MiB limits. This timing
is cap metadata, not a throughput claim or a comparison with the primary.
The result records isolation/bytecode flags, no application imports and an
unchanged Python integer-string guard.

| Retained object | SHA-256 |
|---|---|
| `reference.py` | `b587eab4d53e313d0a8b294e9e28fc599f74291b7f8de970fc51421e072d6a4a` |
| `result.json` | `b7172bf85f6930a8af5e43d4492a21c2e0cdb4255b2ebe080b48f9b331a765e7` |
| `stderr.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| Original `cases.json` | `23b6f928ae94609decfceb86e1c435f8806bb05fb21fe2eb8baf8e9b1efb1b2e` |

Source/result hashes were rechecked unchanged after comparison. The empty
stderr file is part of the evidence and must be retained despite `.log` ignores.

## Actual comparison and artifact checks

The reviewer read all 36 primary case files and compared their entire `worlds`
objects with the independently computed reference: ordered execution vectors,
every activation/incoming snapshot, zero/nonzero final maker remainders and
incoming totals. Every comparison is exact integer/string/null equality, with no
tolerance or selected prefix. Each reference actual-world hash also equals the
primary hash and the independently hashed manual expected object.

All 40 primary paired records agree after renaming only `incoming_id` to the
reference's `event_id`. Requested quantity, baseline/CF external amounts, CF
own fills, both unfilled amounts, signed external displacement, residual
reduction and signed hybrid error agree. Primary identity flags are true and
the flat `paired.jsonl` exactly agrees with the ordered per-case records.

The primary summary hashes canonical compact, sorted-key JSON objects without a
newline. Its manifest hashes raw pretty-JSON file bytes. An initial review check
incorrectly treated the former as raw-file hashes and failed for all cases;
source/encoding inspection resolved that review assumption. Independently
recomputing both declared encodings verifies all 36 canonical case hashes and
all 47 raw inventoried file hashes. No matcher/reference was rerun and no
source/result byte changed to resolve this bookkeeping distinction.

## Structural results and limits

All predeclared structural witnesses pass for sell bases and buy mirrors:

- The shallow Q6 and Q11 cases have identical baseline executed prints, while
  counterfactual own fills are 0 and 5. Executed prints plus a taker ID alone
  cannot certify the supplied full incoming quantity. This is not a claim that
  a complete order feed is insufficient.
- Equal aggregate best external depth of six yields own fills 2 versus 4 when
  the own activation is behind all six versus between the earlier four and
  later two displayed units.
- Same-time activation before versus after the incoming event yields own 2
  versus 0 under the explicitly supplied synthetic sequence order.
- In Q11 followed by Q5, the later CF execution consumes E2:2 and E3:3 that the
  first incoming had displaced. Its second signed external displacement is
  -5, with residual reduction +5 and own fill 0.
- The invalid historical-external-plus-CF-own hybrid has errors +5 then -5 on
  that carry path. Its terminal zero hides two per-incoming errors. Over all 40
  paired rows the signed errors are 24 positive, 2 negative and 14 zero.
- Positive own fills need not imply hybrid budget error: exhausted-depth and
  shallow cases can replace baseline unfilled quantity rather than external
  executions. Every exact paired identity includes that residual term.
- Late activation leaves the new own order resting with five units; the old
  IOC unfilled five is not resurrected. Explicit price limits and multiple-own
  FIFO/partial fills also match their frozen manual paths.

These are constructed, expected mechanics witnesses. The known examples and
mirrors are controls, not independent discoveries or market samples. Both worlds
hold subsequent incoming flow fixed by assumption. No hidden/reserve liquidity,
iceberg replenishment, intra-order interleaving/cancellation, STP, funds or venue
protection logic is modeled; only explicit hard limits are included. No
production fill-model change, real-fill calibration, economics or profitability
conclusion follows.

Only the authorized reference result, empty stderr and this review note were
created in the shared evidence directory by this reviewer. No application,
production engine, old resource study, network data, E1/E2 input, remote write,
access/security change, merge, deployment or trade was used.
