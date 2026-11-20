# Independent physical-artifact and publication review, 2026-11-20

**GO for publication as a bounded constructed-mechanics result.** No material
provenance, numerical, artifact or report-scope blocker was found. This reviewer
did not invoke the primary, unit-token or production matcher. After the explicit
result-review GO, I independently checked recorded executions, priority,
eligibility and quantity ledgers from the frozen events and physical artifacts.
Only this note was authored by this reviewer in the shared worktree.

## Frozen provenance and pre-execution review

The design/manual construction was committed at `a3ffd68`, explicit case bytes
at `3f543ef`, and pre-outcome normalization at `51f89a0`. The unit-token source
was frozen at `0851745`, aggregate matcher/controls at `c067de5`, and the complete
plan at `a487e18b9ab5efc5b4aa12676697eefb4cb7ac5b` before even the fixed
preflight matching controls. The design discloses the known Q11/Q8 examples,
their mirrors and all expected structural outcomes as controls.

| Retained object | SHA-256 |
|---|---|
| `cases.json` | `23b6f928ae94609decfceb86e1c435f8806bb05fb21fe2eb8baf8e9b1efb1b2e` |
| `build_cases.py` | `99cfb6e2eba4a7b38ad7353d3933650ca1527d0311ae1ddbd010fc6430b2fb0e` |
| `docs/INCOMING_REPLAY_DESIGN.md` | `16732218710a2ce465c22ec42a05cbbb9b8a2c69eb91db8b74e7f725fc6945ce` |
| `tools/replay_incoming_orders.py` | `c4eede9260aa25e654367415df9c9f6f39d35ba991844b34aa16d4cce56725e6` |
| `tests/unit/test_incoming_replay.py` | `eb0ca3cc3953bd0b537876d8683c23cdb9d82d502784e27bc7e027493f0db4d9` |
| `plan.json` | `6a12d18cf6ad84aaa83dadc25d0c67fae15c3f9b30e30d84679999efed4434f1` |
| `run/manifest.json` | `f5fe6e6707a93cff84a86a6c6f660723e2260e03227a124cdf7f29ad5df36376` |
| `independent-review/reference.py` | `b587eab4d53e313d0a8b294e9e28fc599f74291b7f8de970fc51421e072d6a4a` |
| `independent-review/result.json` | `b7172bf85f6930a8af5e43d4492a21c2e0cdb4255b2ebe080b48f9b331a765e7` |

Before execution, I checked the case bytes against the exact `3f543ef` Git blob,
the normalized design/builder against `51f89a0`, and the sealed program/control
bytes against `c067de5`. The plan's exact Git bytes, five pins, case order and
declared caps agree. The constructor only serializes literal fill vectors,
mirrors prices/limits and subtracts those declared fills from activation balances;
it does not choose matching executions.

All 38 current production files independently match the inherited C1 inventory,
PR13 `0866bf13e449cd66e3862454eaaf424a5172c33a` Git blobs and PR16 base
`2a96738c080b1a62a32e4306721de703b242477a`. No old market table or resource
worker was needed for this provenance check. The fixed input has 36 cases,
18 recipes and 18 mirrors, with 40 incoming events per world. Its maxima are
18 activated units, five activations and two incoming orders per case.

Static control review confirmed metadata-only plan creation, five live/archived
pin checks, one isolated `-I -B` worker, no overwrite/retry/repair route, durable
completed-case and paired-record prefixes, explicit untested suffixes, terminal
failure status and integrity-only saved-evidence verification. Caps remain
120-second worker, 180-second controller and 16 MiB generated evidence. No
matcher, test or numerical outcome was executed by this reviewer before GO.

## Actual physical verification

Every one of the 47 raw manifest entries matches the physical file hash and
inventory. Including its manifest, the complete original run has 48 files and
543,902 bytes; inventoried payload alone is 537,671 bytes. All five archived
source/input copies match the plan and current bytes. The worker log is empty
and retained. Canonical compact JSON object hashes and raw pretty-JSON file
hashes were independently recomputed according to their separate encodings.

There is exactly one worker attempt, exit zero, with all 36 case IDs completed
in the frozen order, no failed IDs, no current case and no untested suffix.
Isolation flags and before/after no-application-import assertions agree. Recorded
controller/worker lifetimes are 0.234821976 / 0.215989226 seconds, within the
frozen caps. These are budget records, not throughput or capacity measurements.

I checked all 360 event-state snapshots, 168 execution rows, 72 final world
states and 80 incoming walks from the physical case files. For each world:

- Only actually activated makers enter; baseline ignores own activation while
  retaining the chronological unchanged snapshot. Late activation after the
  final incoming still appears in the final book.
- Every fill references an active, eligible maker at its declared price/owner.
  At each recorded execution the maker is earliest eligible by price then
  original activation `(timestamp, sequence)`; a partially filled maker cannot
  be skipped while incoming budget remains. Both hard-limit directions pass.
- Each incoming satisfies `Q = external + own + unfilled`. Positive unfilled
  size occurs only when no eligible displayed balance remains, and never becomes
  a resting aggressor or executes on later activation.
- At every event, entered quantity equals cumulative recorded fills plus
  remaining quantity for every maker. Zero balances remain represented. Every
  remainder snapshot, ordered fill vector, final balance and total equals the
  frozen manual oracle exactly, without tolerance or selected prefixes.

All 40 paired JSONL records equal the ordered per-case records. Requested size,
both external amounts, CF own fills, residuals, signed displacement and signed
hybrid error were recomputed independently. Every exact paired identity holds:

```text
CF own = baseline external - CF external
       + baseline unfilled - CF unfilled
```

The derived invalid-hybrid errors are 24 positive, two negative and 14 zero.
They are expected negative-control diagnostics, not unexpected primary failures
or a third matching world's results. In displaced_carry the errors are +5 then
-5, with terminal zero. The second external displacement is -5, residual
reduction +5 and CF own zero; rejecting negative displacement would be wrong.
Positive own fills can coexist with zero hybrid quantity error in shallow11 and
exhausted17. Quantity totals alone therefore do not replace full path/priority
checks.

## Independent reference and structural witnesses

The current unit-token source matches its pre-outcome `0851745` Git blob and
stated hash; its retained result and empty stderr match their recorded hashes.
The reference reports one complete pass, no application imports, unchanged
integer-string guard, no mismatch and no untested suffix. I independently
compared every reference world object, expected/actual hash, hybrid vector and
common paired field against the primary physical records and frozen oracle.
All 36 cases, 72 paths, 80 walks and 40 paired rows agree. This is corroboration
of the same constructed family, not additional independent observations.

Both sell cases and buy mirrors exhibit the preregistered structural results:

- Shallow Q6/Q11 have identical executed baseline prints and taker ID, but
  baseline residuals 0/5 and CF own fills 0/5. Entire baseline traces and a
  complete order feed are not claimed identical.
- Known8/split_time share aggregate best external size six but different
  activation histories; CF own size is two/four. This does not establish
  insufficiency of a complete activation feed.
- Explicit same-time sequence gives own two/zero for activation before/after.
- The Q11-to-Q5 carry path later executes the previously displaced E2:2 and E3:3
  in CF, while baseline is empty. Future incoming flow is fixed by assumption.
- Late activation leaves own five resting and does not resurrect terminated
  incoming unfilled five. Multiple own makers, partial priority and hard limits
  also agree with their declared manual paths.

## Report and publication scope

The report's numerical tables, counts, signs, mirror/exposure labels and limits
agree with the physical evidence. Known hand examples and mirrors are controls,
not discoveries or holdouts. No cost/P&L or production-replacement result is
claimed. The deliberate hybrid remains clearly distinct from production and
from a third actually replayed world.

Local logs support 37 preflight controls, 727 regression passes, 90.86%
application coverage, Ruff lint/format, mypy on 37 modules and the standard
300-event synthetic smoke. The local source proof identifies this worktree and
all 38 C1 hashes. These checks are software verification, not empirical sample
counts. The CI addition separates integrity-only verification from an explicit
fixed-family repeat and retains original plus CI evidence; inherited verification
remains disclosed. Remote exact-head CI is accurately pending at this local seal.

All 66 preexisting evidence files match their Git index blobs, including all
nine `.log` files and empty logs. No bytecode/cache file is included. Every local
validation log exists; after this note is written all 18 local report links
resolve. This note is the one additional file for the publisher to stage. A
bounded scan of the new bundle found no private-key, AWS-key or GitHub-token
patterns; no licensed/private market data was observed. Workspace/interpreter
paths are normal provenance metadata.

Actual venue execution is not calibrated. Hidden/reserve/iceberg behavior,
intra-walk interleaving/cancellation, STP, funds and venue protection rules remain
outside the contract. Supplied full incoming size and unchanged future flow are
synthetic assumptions. The case family does not make those quantities observable
from real trade-only/L2 records, nor resolve production performance/output growth.
Final commit inclusion, prior-head preservation and terminal remote CI are
separate publication checks for the parent task. No source/raw-result edits,
reruns, data acquisition, access/security change, merge, deployment or trade were
performed by this reviewer.
