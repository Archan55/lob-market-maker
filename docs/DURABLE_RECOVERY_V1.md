# Bounded offline durable client recovery v1

This opt-in path recovers a stopped client's delivered-evidence knowledge from
files after process death. It preserves fill identity, accounting, quantity
bounds and receipt offset together. It emits no commands and does not restore
an active strategy, stop controller, scheduler or venue. Ordinary backtests and
all previous versioned studies retain their behavior.

The [contract, hypotheses, oracles and finite failure matrix](DURABLE_RECOVERY_CONTRACT_V1.md)
were written before experiments. The source baseline is PR2
`8391031107b0f18c4c0176099f9098d4195ed88b`, based on unchanged draft PR1
`d4104b70660976c6d59fe8915a75ac5616bdecd3`. Initial implementation
`56049ce2a816be3f41ff7a8f87c4d0fd6aeba353` is retained for independent
before/after review reproduction. No previous engine defect is newly claimed.

## What survives restart

`lobmm.durable_recovery` accepts an already durable, immutable JSON inbox:
previously sent order intents plus execution reports and frozen reconciliation
responses in actual delivery order. The caller supplies the expected session
and SHA-256 of the exact inbox bytes. Each receipt has a stable envelope ID;
report, query, fill and venue-order identities bind immutable payloads. An
identical envelope may recur at a later notification time. Identity excludes
that delivery time; the earliest delivered fill detail remains its notification.
Conflicting payload reuse blocks recovery. Stable client and venue order IDs
are assumed, with one venue order identity per saved intent.

The format is `durable-client-v1`, strict JSON with unknown/duplicate fields
rejected. There are at most 256 intents, 4096 receipts, and 4 MiB per file.
Costs have at most 64 coefficient digits and exponents in [-64,64]; exact
summation uses a fixed Decimal context with 256 digits, independent of the
caller's precision, exponent limits or traps. Integer cash is tick notional;
no P&L, venue mark or liquidation value is inferred from this client ledger.

The checkpoint envelope contains `sha256` and `checkpoint`. Its document binds
`version`, `session_id`, `inbox_sha256`, applied `offset`, `prefix_sha256`, and
`state`. Offset counts fully applied receipts. State contains quantity bounds,
terminal evidence, fill detail, earliest notification times, identity maps and
the exact ledger. A single atomic replacement commits all these fields.
`open()` checks the checksum and schema, rebuilds the immutable prefix and
requires exact equality before exposing state. Prefix reconstruction is an
intentional bounded offline verification cost. It is not a low-latency design.

`create()` is explicit initialization and refuses an existing checkpoint.
`open()` refuses missing state; direct construction is blocked. Temporary files
use exclusive creation in the destination directory. Persistence writes a
complete temporary file, fsyncs it, replaces the checkpoint and fsyncs its parent
directory before `advance()` returns acknowledgment. Storage failure poisons
the instance: reads and further advances require reopening. A directory-fsync
failure may leave a new valid prefix on disk despite no acknowledgment.

## Process-death results

A parent sent **SIGKILL** to an actual waiting child; another fresh interpreter
then opened the files and replayed. No client object or memory clone crossed
the restart. Every boundary was exercised once while adding fill economics and
once while adding terminal quantity evidence with missing detail: **14 deaths**.

| Interruption boundary | Recovered offset | Result after replay |
|---|---:|---|
| Before temporary creation | 1 | Offset 3, matches uninterrupted bytes |
| Half a temporary write | 1 | Same |
| Complete write, before file fsync | 1 | Same |
| After file fsync, before replace | 1 | Same |
| After replace, before directory fsync | 2 | Same |
| After directory fsync, before return | 2 | Same |
| After `advance()` returned acknowledgment | 2 | Same |

The post-return case pauses in the worker after the API has returned. Orphan
temporary files are ignored. Old/new committed checkpoints and uninterrupted
checkpoint hashes are retained in `death-evidence.json`. Uninterrupted byte
equality is labeled same-implementation consistency; it supplements the
independent accounting and identity oracle below. These observations concern
process death on the tested Linux filesystem, not host power loss.

## Evidence, accounting and unknown outcomes

**24 full-loop cases / 106 received envelopes / 130 prefixes** cover the twelve
prior reordering modes, mirrored bid/ask. Receipts are captured before transport
duplicate suppression and preserve actual notification order. They include
newer terminal before older live snapshots, reversed partials, late originals
after query recovery, duplicated responses, absent/truncated snapshots and
finite horizon/live residual cases. The fold is independent of strategy state;
these captures use fixed-spread strategy. The prior three-strategy studies are
preserved separately.

At every prefix an independent witness intersects sent-quantity evidence,
unions payload-consistent fill IDs and checks signed inventory, integer cash,
Decimal costs, notification times and identity ownership. It does not call the
production knowledge, journal, exchange or portfolio as an oracle. Every prefix
is reopened from disk. Six deliberate corruptions of witness outputs are
rejected. A separate independently authored five-unit, two-plus-three-fill
fixture checks both sides without importing the study fixture or oracle.

**20 cases finish with complete delivered client knowledge; four remain
incomplete.** Reversed-partial cases and live-beyond-tape cases in each direction
retain known inventory +4/-4 and bounds [4,6]/[-6,-4]. Their covered synthetic
venue inventory is +4/-4. Client knowledge cannot rule out the remaining two
units; matching the covered venue inventory is not proof of a terminal outcome.
Restart neither invents terminal evidence nor releases those reservations.

The terminal-only crash witness establishes four executed units while only two
have delivered detail. Recovery keeps the two-unit ledger and discloses two
missing execution units. In the independent five-unit witness, terminal state
establishes five units while only two units and fee 0.17 are known. A later
original supplies the other three exactly once; older live/query copies cannot
resurrect reservations or duplicate fees. Unknown-order rejection remains
insufficient to prove non-execution.

## Independent review and rejection controls

The separate review suite has **58 controls**, including invalid committed
state even when its outer checksum is recomputed, malformed/truncated/duplicate
JSON, wrong session/inbox/version, offset/prefix/state mismatch, identity
collisions, inconsistent quantities and report shapes, noncausal evidence,
strict scalar types, nonfinite/out-of-bound costs and file/count limits.
Injected actual stream-write, file-fsync, replace and directory-fsync errors
produce no acknowledgment and require safe reopen. These are disk/reopen and
storage-error tests; only the subprocess matrix is claimed as process death.

Five concrete defects in the **new initial implementation** reproduce against
`56049ce2a816be3f41ff7a8f87c4d0fd6aeba353`: caller Decimal context rounded fee
0.300003 to 0.3000; a false complete-detail claim passed; contradictory terminal
quantity was masked by the remaining-quantity minimum; one intent accepted two
venue IDs; and an ACCEPTED/FILLED report mismatch retired exposure. Independent
identical tests yield **five failures before / five passes after**. Exact
terminal quantity/time bindings, bijective order identity, detail/state and
report-shape validation, and fixed-context sums correct these findings. Direct
constructor bypass and post-terminal fill-time controls also pass. The review
ended with no blocking finding. These are prepublication development defects,
not failures newly attributed to pinned PR2's old behavior.

## Preservation and reproducibility

All **11,708 prior stable artifacts** match exact `8391031` in one installed
dependency environment: latency 600, exposure 378, client-stop 980, transport
2550, reconciliation 5040, reordering 2160. All **30 prior documentation files**
are unchanged. Earlier defect helpers still reproduce their versioned findings.
Three durable studies agree across **128 persisted artifact pairs** per
comparison and audit JSON; temporary filenames and wall-clock timing are
excluded. The published [JSON audit](DURABLE_RECOVERY_V1.json) SHA-256 is
`fe54398ae7fc3c41a5dd6240021333f5dc8a81d78113d9f15903636fb8e85713`.

Frozen-source cloud validation on Python 3.12.14 passed **1,389 tests with
91.94% branch-aware package coverage**, Ruff lint/format, strict mypy, all
preservation and before/after helpers, the 300-event smoke backtest and canonical
nine-case queue audit. Canonical JSON matches its saved report byte for byte;
Markdown matches after its expected published JSON filename substitution.

From the repository root in the saved cloud environment, use Python 3.12+:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/lobmm
.venv/bin/python -m pytest tests/integration/test_durable_recovery_v1.py tests/integration/test_durable_recovery_independent.py -q --basetemp=experiments/durable-focused-tmp --durable-study-output experiments/durable-focused
.venv/bin/python tools/reproduce_durable_review_defects.py
.venv/bin/python tools/verify_durable_recovery.py experiments/durable-fresh-verification
.venv/bin/python tools/verify_reordering_evidence.py
.venv/bin/python tools/verify_reconciliation_evidence.py
.venv/bin/python tools/verify_transport_evidence.py
.venv/bin/python tools/reproduce_exposure_defects.py
.venv/bin/python tools/verify_client_stop_baseline.py
.venv/bin/python tools/reproduce_reordering_defect.py
.venv/bin/python -m pytest --cov=lobmm --cov-report=term-missing --basetemp=experiments/full-repro-tmp
.venv/bin/python -m lobmm.cli backtest --config configs/ci.yaml --run-name ci-smoke
.venv/bin/python -m lobmm.cli experiment --config configs/queue_cancellation.yaml --name queue-cancellation --latency-multipliers 0.5,1,2
.venv/bin/python -m lobmm.cli audit-queue-study --experiment experiments/queue-cancellation --config configs/queue_cancellation.yaml
```

Use fresh study output directories; `write_inbox` intentionally refuses to
replace an existing inbox. The CI workflow runs these gates on the exact PR
head and uploads the inputs, checkpoints, prefix traces, process-death evidence,
preservation pairs and before/after review logs. Dependency versions, Python
version and checkout identity are retained alongside that evidence. CI links
and its verified commit are recorded in the draft PR description.

## Limits

This work assumes an already complete immutable inbox for the supplied client
delivery history, stable never-reused order identities, a single writer, and
same-directory POSIX replacement/fsync semantics. It does not record live
receipts, recover concurrent writes, reconstruct lost orders/requests, resume
cancellation work, rebuild simulator state, or prove that delivered evidence
contains all venue executions. A valid older checkpoint can replay; there is
no independent anti-rollback watermark. Checksums detect accidents, not
malicious rewriting. Power failure, disk/controller failure, other filesystems
and retention loss remain untested. Acknowledgment durability depends on the
filesystem honoring fsync. Finite synthetic evidence gives no production
readiness, calibrated venue behavior or profitability claim.
