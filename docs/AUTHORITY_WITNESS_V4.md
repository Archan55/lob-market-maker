# Cancellation-only authority witness and issuance v4

Synthetic, uncalibrated research. All new entry through the v4 facade is
quarantined, including with a copied current token. No live readiness,
exactly-once, process exclusivity or profitability claim.

[Contract fixed before implementation](AUTHORITY_WITNESS_CONTRACT_V4.md) ·
[Independent review](AUTHORITY_WITNESS_INDEPENDENT_V4.md) ·
[Canonical verification manifest](AUTHORITY_WITNESS_V4.json)

## What the next subset establishes

`WitnessedAccountAuthorityV4` is a separate opt-in cancellation-only facade over
the unchanged v3 synthetic venue. A separately retained SQLite witness records
the digest of the entire venue event document, operation watermark, pending
write intent and immutable token issuance requests. This is a conditional model:
retaining two files on the same host does not itself make their failure domains
independent. Initial enrollment trusts a quiescent available venue baseline.

For each mutation, the witness commits an intent before the venue commits. The
venue and witness then finalize under serialized transactions. There is no
cross-store atomic-commit claim. Death in the gap leaves a pending intent and
freezes the facade, even when venue bytes match the saved old/proposed hash.
Clearing that intent automatically would discard evidence that an effect might
have committed and subsequently been rolled back. Expected stale, malformed or
conflicting requests are rejected before durable intent.

Issuance request IDs bind account, owner, session, strategy and observed
expected generation. A finalized issuance response lost to process death is
recoverable using the same immutable request ID without another generation or
watermark increment. Historical lookup returns the original token, which cannot
cancel after a newer takeover. Exact retained issuance membership rejects
spliced wrappers and foreign witness/incarnation fields. Different simultaneous
issuers for one observed generation produce one winner. Identical simultaneous
requests both obtain the same token. Tokens remain transferable simulated
credentials, so these results do not identify one operating-system writer.

## Useful quantity and failure findings

The 16 primary cases and nine independent cases include actual competing
interpreters, held commands, lost responses and eight real SIGKILL deaths.
Canonical JSON retains raw venue inputs/events, witness states, reports and
scalar outcomes. Operating-system-selected race chronology is preserved
separately as JSONL, not asserted to repeat byte for byte.

| Evidence/fault | Observed result |
| --- | --- |
| Authority restored, witness retained | Digest mismatch; read/issuance/cancellation facade blocks. |
| Witness restored behind authority | Digest mismatch; no automatic catch-up. |
| Saved client token restored alone | Old cancellation rejected; current token cancels four residual units after two fills. This restores a saved token, not a v2 journal. |
| Both stores restored to a matching old prefix | Undetectable: primary observer saw two fills / inventory +2 / cash -202, while restored venue shows zero fills and cancels all six old units. Independent observer saw three / +3 / -339. |
| Missing witness or changed configured incarnation | Refused; no automatic recreation/enrollment. |
| Byte-identical authority-file replacement | Accepted logical bytes; physical lineage is unobservable. Fresh empty same-ID authority is rejected. |
| Death before intent | Old state survives; issuance/cancellation may proceed. |
| Death after intent, before authority commit | Pending intent blocks; old accepted order can still execute. |
| Death after authority commit, before finalization | Committed generation survives but pending intent blocks recovery; no abort/reissue. |
| Death after finalization, before return | Original issuance token recovered without second takeover. |
| Copied current token in two actual processes | Both obtain the same cancellation result; six units cancelled once, zero executed. No process-exclusivity claim. |
| Direct v3 mutation bypasses facade | Extra account retains six executable units; witness detects changed bytes afterward, but cannot undo the bypass. |

The primary paired bid/ask cases execute two of six units at 101 and cancel four.
Inventory/cash are respectively `+2/-202` and `-2/+202`. A held unaccepted new
command processed by a separate old process after takeover is refused; the one
already accepted order still needs cancellation. A lost cancellation return and
same-command retry preserve the original result and quantities.

The independent mixed book accepts seven bid units at 113 and five ask units
at 127 before takeover. Three units execute on each side across takeover;
four bid and two ask units are cancelled. Separate raw-input arithmetic gives
inventory zero and cash `-339 + 381 = +42` ticks, with no executable residual.
It also checks unique fill-report sums without using the production fold or
primary oracle. Its cancellation auditor is bounded to client-order IDs.

Fail-closed cancellation is an availability cost, not a safe-trading conclusion.
In pending-intent cases, direct modeled venue market progress outside the frozen
facade executes all six primary units (`+6/-606`) or seven independently reviewed
units (`+7/-791`). Locks cannot undo those fills or flatten the resulting inventory.

## Pinned implementation defect and correction

Initial source is pinned at `5ca2c928a7ad0bf9b2baf68051a7ffae7dc2baf4`.
The independent regression was committed unchanged at `615cd3c`: a correctly
checksummed stored issuance had a request owner contradicting its token.
The initial implementation allowed cancellation; the corrected implementation
rejects inconsistent request/token fields, generation and issuance chronology
before any effect. The unchanged regression fails before and passes after. The correction is pinned
at `d4dac9167e822ab9f73bb880e2669d3b3e67f224`.

The unchanged executable probe independently reconstructs seven cancelled units
before hardening versus seven residual units with unchanged venue bytes afterward;
both have zero inventory/cash. Rejection protects evidence consistency and can
reduce cancellation availability. This is an injected noncanonical malformed
state, not a reachable canonical execution or an authentication exploit. Ordinary
checksums do not establish protection against a party able to rewrite both stores.

`tools/reproduce_authority_witness_defect.py` records exact source/test/probe
SHA-256 values, raw malformed witness and venue documents, independent scalar
reconstruction, pytest output and JUnit before/after results.

## Reproduction and preservation

From the saved repository, using the same dependency environment:

```sh
.venv/bin/python -m pytest tests/integration/test_authority_witness_v4.py tests/integration/test_authority_witness_independent.py -q --authority-witness-output experiments/authority-witness-v4
.venv/bin/python tools/verify_authority_witness.py
.venv/bin/python tools/reproduce_authority_witness_defect.py
```

The verifier reproduces 27 canonical JSON files byte for byte. It compares
33 prior v3 JSON artifacts against exact baseline PR2
`91675f77738dda6940072cdaba565c455ae2f274`, and verifies all 46 prior source
files and all 45 prior documentation files unchanged. Previous v3 verification
also confirms its 33 repeat artifacts, 326 prior v2 artifacts and 41 older
prior documents unchanged. The original v3 negative counterexamples and all
prior APIs, reports, pinned regressions and quarantine behavior remain intact.

The full local aggregate passes **1,556 tests / 91.77% branch-aware coverage**;
Ruff lint/format and strict mypy pass. Smoke replay and the canonical nine-case
queue experiment/audit pass. The unchanged pinned regression fails before and
passes after hardening.

The CI workflow runs the aggregate, new repeats and pinned defect reproducer,
all previous verification helpers, smoke replay and canonical queue experiment;
it uploads the new raw evidence alongside prior evidence archives. Exact-head
checkout identity is recorded independently of the PR merge reference.

## Practical boundary and next step

A chosen witness must actually survive the authority/client fault being modeled.
There is no automatically discovered global namespace, physical-incarnation
attestation, external account inventory universe, sole-writer identity, power-loss
proof, hostile storage guarantee or live venue adapter. Alternate enrollment and
direct underlying calls remain outside the facade. Limits are 64 issuance records,
4096 mutations and 4 MiB witness bytes plus unchanged v3 bounds; exhaustion blocks.

Joint matching-prefix rollback presents the same visible bytes as a history with
no later execution. Detecting that history requires a further independently
retained observation, whose availability/retention cannot be invented here.
The next justified research step is to specify that evidence and a read-only
resolution protocol for pending intents while modeling autonomous accepted-order
fills. Cancellation liveness and exposure must be established before considering
entry recovery. Entry remains quarantined.
