# Independent review of authority witnessing and issuance v4

This review starts from the pre-experiment contract and initial implementation
`5ca2c928a7ad0bf9b2baf68051a7ffae7dc2baf4`. It does not import the production
`_fold`, `_apply`, or primary experiment oracle. Inputs are synthetic and
uncalibrated. New entry remains quarantined throughout the v4 facade.

## Assumptions challenged before implementation review

The witness must retain the digest of the entire authoritative event document,
including all accounts. Checking only a generation or the currently requested
account would miss rollback of fills, command identities, or another account.
The witness must validate before returning an historical issuance, and an
issuance ID must bind the exact immutable owner/session/strategy/account and
expected generation. An old successful issuance lookup cannot reactivate its
old token. A current token spliced into another issuance wrapper must also fail.

An intent must commit before any venue effect. Otherwise death between the
venue effect and witness update leaves a believable old watermark. Conversely,
a surviving intent cannot safely be cleared merely because the venue currently
matches either old or proposed bytes: the proposed effect might have committed
and then been rolled back. The implemented choice to freeze progress preserves
this distinction but can prevent cancellation while old accepted orders execute.
Expected stale/conflicting requests must fail before intent reservation so that
ordinary rejected contention does not itself freeze cancellation.

SQLite serialization represents this simulator's authority transaction. It
does not fence a real venue. Separate local SQLite files do not prove independent
retention; that is an explicit fault assumption. Initial enrollment trusts the
available event document and cannot attest to earlier lost execution. Nor can
the facade prove that the operator selected its only witness, prevent enrollment
of an alternate witness, or identify a physical database replacement with exactly
the same logical bytes. The incarnation is a logical configured identity.

## Independently reconstructed outcomes

`tools/witness_independent_v4.py` reads authority SQLite JSON directly. Starting
from raw claim, command and trade inputs, it maintains its own owner/generation,
immutable command/client identity sets, FIFO quantity residuals, and signed
integer inventory/cash. It compares those results with public snapshots and
separately summed unique report fills. The reviewed cases cancel by client ID;
the auditor explicitly refuses exchange-ID cancellation without an independently
observed mapping rather than importing production's order-ID algorithm. It is
a bounded second calculation, not independent proof that the modeled venue has
complete external inventory or implements a real market.

The independently authored tests in
`tests/integration/test_authority_witness_independent.py` establish:

- Seven bid units at 113 and five ask units at 127 are accepted before takeover.
  Before takeover, two bid units and one ask unit execute. After takeover, one
  further bid unit and two further ask units execute. Cancelling four bid units
  and two ask units leaves no executable residual. Independent arithmetic gives
  inventory `3 - 3 = 0` and cash `-3*113 + 3*127 = +42` ticks. Duplicate immutable
  cancellations do not change those quantities. The held unaccepted new command
  never appears in the authoritative command history.
- Identical issuance retry returns the same token without changing retained
  witness bytes. Conflicting request reuse and distinct stale-generation
  contenders leave evidence unchanged. Historical retry after a later takeover
  still returns the old token, while cancellation with that token is refused.
  Wrong witness/incarnation and spliced issuance wrappers are also refused.
- Two independently started interpreters are both live before competing
  expected-generation-one issuance requests are sent. Exactly one succeeds;
  generation becomes two and the witness advances once with one issuance.
  Scheduler-dependent inputs, replies and complete raw retained chronology are
  saved separately in `independent/runtime-races.jsonl`; deterministic summaries
  do not claim a predetermined winning process.
- Real SIGKILL at `before_intent`, `after_intent`, `after_authority_commit` and
  `after_finalize` distinguishes complete old state, pending old state, pending
  committed state, and fully finalized state. At the two pending stages, open
  and issuance retry refuse. Direct underlying v3 market progress, explicitly
  outside the frozen facade, executes all seven still-accepted bid units: signed
  inventory `+7`, cash `-791`. In the recoverable before-intent and finalized
  cases, successful issuance recovery and cancellation leave later fills zero.
  A successful response was never required to retain the committed generation.

## Preserved impossibility and namespace counterexamples

The joint rollback experiment keeps an observer's three-unit fill report, then
restores both authoritative and witness stores to the same earlier prefix.
Their visible logical bytes exactly match the no-execution history, and the
old issuance is again accepted for lookup. The facade reconstructs zero
executed inventory/cash; the separately retained observer saw `+3` inventory
and `-339` cash ticks. That observer is deliberately outside both restored
stores. Without consulting such a third retained observation, the facade cannot
distinguish these histories. This is missing information, not a test failure
to repair with another local lock.

A separate namespace case replaces the authority file with a copy containing
the exact same logical body and digest. Open succeeds, demonstrating that
logical equality does not attest to physical file lineage. Replacing it with
a fresh empty authority using the same configured authority ID changes the
digest and is rejected. Neither this rejection nor the configured incarnation
establishes a global namespace or prevents alternate enrollment. Copied current
tokens remain transferable; facade quarantine prevents their use for new entry
but does not identify one operating-system writer.

## Pinned malformed-evidence defect

Initial implementation `5ca2c928a7ad0bf9b2baf68051a7ffae7dc2baf4` validates stored
issuance requests and tokens separately without checking that their owner fields
agree. The independent regression changes a retained request owner to a
contradictory string, recomputes its ordinary checksum, and attempts cancellation
with the original token. The initial implementation accepts it and changes the
venue, violating the contract's requirement to reject malformed evidence before
effects. The unchanged regression fails with `DID NOT RAISE RecoveryError`.

This injected state is not produced by canonical successful execution, and a
random bit flip would ordinarily fail the checksum first. The finding is a
semantic evidence-validation defect under the explicitly tested malformed-state
contract; it does not establish hostile-tamper resistance or a credential attack.
The regression additionally requires unchanged venue bytes when rejection is
correct. Source hashes and pinned before/after results must accompany the fix.

The reviewer files were pinned unchanged in commit `615cd3c` before the
production fix. The fixed validator checks request/context equality, issued
generation against expected generation, issuance count against watermark, and
retained per-account issuance generation order. Independent rerun of
`tools/reproduce_authority_witness_defect.py` confirms the original source fails
the unchanged selected test and accepts cancellation on contradictory evidence;
the reviewed fixed source passes and leaves authoritative bytes unchanged.

- Initial production source SHA-256:
  `eae99bd331a4a451ae247fdbdaff3b67ce56403c9edf6fffcca03e880027e50d`.
- Reviewed fixed production source SHA-256:
  `f1465012f6ffedf2fc061862079c16e976bf97c0e7caee024f43640ac1a90d25`.
- Unchanged independent test SHA-256:
  `beb47182aa3b4effd33313fc588686f66a5f96fed0fe743ea1f8b3be10830c07`.
- Unchanged raw auditor/worker SHA-256:
  `a84d0d20a9ae50d995eb6f146b4ab9602cc3cb838df29de10780ed9c586bf6d7`.

All nine independent cases pass against this fixed source. The eight cases
unrelated to the malformed-evidence regression were also run twice: their nine
canonical JSON files, including the independent audit manifest, repeat exactly.

## Cross-check of primary raw evidence

The reviewer separately reconstructs primary archived authority event inputs
with its own integer accountant and independently hashes their canonical JSON
against retained witness digests. The primary experiment's oracle is not used.
The resulting input-artifact hashes and full reconstructed quantities are
retained in `docs/AUTHORITY_WITNESS_PRIMARY_AUDIT_V4.json`. For its six-unit bid
at 101:

| Primary case | Generation | Inventory / cash ticks | Executed / cancelled / remaining | Digest agrees |
| --- | ---: | ---: | ---: | --- |
| Authority-only rollback | 2 | 0 / 0 | 0 / 0 / 6 | No |
| Witness-only rollback | 3 | +2 / -202 | 2 / 0 / 4 | No |
| Saved-client-token rollback | 3 | +2 / -202 | 2 / 4 / 0 | Yes |
| Matching joint rollback, then cancel | 2 | 0 / 0 | 0 / 6 / 0 | Yes |
| Two processes copying the current token | 2 | 0 / 0 | 0 / 6 / 0 | Yes |

The joint rollback case additionally retains two externally observed fills
outside the restored stores, illustrating the accounting history the visible
model loses. The saved-client-token case tests an old token after takeover;
it does not itself restore a v2 client journal. In the copied-token case both
live processes receive the same retained cancellation report, and both request
events remain visible. This demonstrates transferable authority and duplicate
command semantics, not process exclusivity or an exactly-once transport claim.

The primary bid and ask exposure cases independently reconstruct `+2/-2`
inventory and `-202/+202` cash ticks respectively, with four remaining units
cancelled. The held unaccepted command leaves only the one pre-existing order,
with all six units cancelled and zero fills. A direct v3 bypass adds a separate
account with six executable units; the full-body witness digest then disagrees.
No remaining observed implementation violation blocks the declared conditional
subset. Entry recovery and progress during lost evidence remain outside the
supported result and require additional evidence or a different protocol.

Regenerate that audit after producing the primary canonical artifacts with
`python -m pytest tests/integration/test_authority_witness_v4.py
--authority-witness-output experiments/authority-witness-v4`, then run from the
repository root:

```python
import hashlib
import json
from pathlib import Path

from tools.witness_independent_v4 import reconstruct

root = Path("experiments/authority-witness-v4/primary")
cases = [
    "rollback-venue",
    "rollback-witness",
    "rollback-client",
    "rollback-both",
    "unavailable-namespace-copied-token",
    "direct-v3-bypass",
    "held-unaccepted",
    "exposure-1",
    "exposure--1",
]
summary = []
for name in cases:
    path = root / ("case-" + name + ".json")
    data = json.loads(path.read_text())
    canonical = (
        json.dumps(
            data["authority"], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode()
    matches = (
        hashlib.sha256(canonical).hexdigest() == data["witness"]["authority_digest"]
    )
    assert matches == (
        name not in ["rollback-venue", "rollback-witness", "direct-v3-bypass"]
    )
    summary.append(
        {
            "case": name,
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "independent_from_raw_inputs": reconstruct(data["authority"]),
            "digest_matches_witness": matches,
            "external_fill_quantity": sum(
                report["fill"]["quantity"]
                for report in data.get("external_executed", [])
            ),
        }
    )
Path("docs/AUTHORITY_WITNESS_PRIMARY_AUDIT_V4.json").write_text(
    json.dumps(summary, sort_keys=True, indent=2) + "\n"
)
```

Canonical reviewer JSON archives include raw event inputs, witness evidence,
independent quantities and scalar comparisons. Runtime race JSONL remains
separate because operating-system scheduling need not repeat. These findings
support only the bounded cancellation-only model with its selected independently
retained witness. They do not establish entry recovery, live readiness,
exactly-once effects, profitability, or safe trading during cancellation freezes.
