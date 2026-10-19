# Read-only pending cancellation classification

The predefined question has a **conditional positive answer for exact historical
transitions**, and a negative answer for fill-only evidence or cancellation
recovery. A trusted complete raw transition archive, an independently fresh
closed cut, a terminal pending writer and an exact candidate preimage permit
classification after accepted-order fills change the current authority hash.
Those evidence properties are assumptions of this bounded experiment. No
independent storage infrastructure, live execution evidence source or recovery
protocol is constructed here. Entry and pending-intent recovery remain disabled.

[Pre-outcome contract](PENDING_INTENT_EVIDENCE_CONTRACT.md), committed at
`0e03c3d` · [Repeat/preservation manifest](PENDING_INTENT_EVIDENCE.json) ·
[Independent review](PENDING_INTENT_INDEPENDENT_REVIEW.md)

## What was implemented and what the evidence means

`tools/pending_intent_evidence.py` accepts data only and has no file, command or
authority capability. It validates the complete bounded raw history and its
before/after full-document hashes. The capsule binds authority/witness/logical
incarnation, pending watermark, baseline and exact cancellation input to the
old/proposed hashes. The current v4 witness does not retain that raw capsule;
supplying it is an additional experimental evidence requirement.

The results are `observed_exact_commit`, `certified_no_commit`, or `unresolved`,
with a separate assessment of current retained history. `certified_no_commit`
means the exact proposed append did not occur after the trusted baseline by the
terminal cut. It does not mean an immutable cancellation never executed before
baseline, that the target remains executable, or that retry is authorized.
Duplicate commands can return an older result while creating a different raw
append. A well-formed restored authority prefix does not erase an independently
proven historical commit. Malformed inputs and internally contradictory archive
evidence abstain. Every result has `authorizes_recovery=false`.

The fresh cut must be supplied independently of the archive being checked. It
asserts coverage of **all authority transitions**, including cancellations with
no fills, claims, no-ops and all accounts, and that the pending writer cannot
later commit. A post-commit observer append can die before recording the effect;
that implementation would not satisfy the completeness assumption. Hashes and
another SQLite file do not establish retention independence or freshness.

## Fixed experiment and raw quantity findings

The 24-case matrix accepts a seven-unit target and five-unit same-side sibling
before takeover, varies bid/ask side, intent-only/post-authority-commit stop,
initial target fills of zero/two, and later trade budgets of zero/one/seven.
After classification a further seven-unit market budget executes independently
of the frozen facade. The sibling supplies genuine descendant fills after a
committed target cancellation; a cancelled target cannot do so.

All 24 complete cases classify the exact transition correctly under the stated
oracle assumptions. **16 cases have current hashes matching neither the old nor
proposed pending hash** after autonomous progress. Historical transition prefixes
still settle those cases. Classification leaves both SQLite stores byte-identical.
Snapshot, facade trade, cancel retry, sibling cancel and new entry all remain
blocked afterward. Direct modeled venue execution continues after the injected
caller stop releases its local SQLite transaction.

The experiment uses synchronous fail-stop exceptions, explicitly not additional
real SIGKILL tests. V4 holds the authority transaction while the intent is being
prepared; this does not model a real venue filling concurrently through that
local lock. Existing real-process-death evidence remains in the unchanged v4
study.

The following quantities are reconstructed directly from raw accepted-command,
cancellation and market-trade inputs. A separate reviewer implemented another
integer/FIFO accountant and checked every matrix case against closed formulas,
signed cash, residual identities and inventory extrema. Neither accountant calls
the production fold, exchange replay or primary study oracle.

| Target side / initial fills / later budget | Target cancel committed | Final filled / cancelled / executable units | Inventory | Cash ticks | Live quantity-time, t20–50 |
| --- | --- | --- | ---: | ---: | ---: |
| Bid / 0 / 0 | No | 7 / 0 / 5 | +7 | −791 | 290 |
| Bid / 0 / 7 | No | 12 / 0 / 0 | +12 | −1336 | 170 |
| Bid / 0 / 0 | Yes | 5 / 7 / 0 | +5 | −545 | 100 |
| Bid / 2 / 7 | Yes | 7 / 5 / 0 | +7 | −771 | 50 |
| Ask / 0 / 7 | No | 12 / 0 / 0 | −12 | +1544 | 170 |
| Ask / 2 / 7 | Yes | 7 / 5 / 0 | −7 | +909 | 50 |

Quantity-time is the integral of executable units over the fixed synthetic
timestamp intervals [20,30), [30,40), [40,50), in unit-nanoseconds. Timestamps and
prices are uncalibrated. Bid prices are 113/109; ask prices are 127/131.
Initial attainable inventory is [0,+12] for the unfilled bid pair and [−12,0]
for the ask pair; opposing executable quantities would not be netted.

**Read-only classification produces zero cancellation-liveness improvement.**
In the uncommitted cases the target remains exposed until fills; in committed
cases the sibling remains exposed. Zero terminal residual caused by fills is
not cancellation success. These rows compare preexisting commit histories, not
the effect of a recovery policy. Cash is signed tick notional, not profit.
The v3 report's `remaining_quantity=original-filled` can include cancelled units;
the auditor uses raw cancellation events and executable order quantity instead.

## Negative findings retained

Eight detectable faults—missing archive, stale cut, sequence gap, contradictory
raw effect, wrong incarnation, nonterminal writer, observer gap and fill-only
coverage—return `unresolved`. The independent suite also rejects a candidate
command found at a different prefix and checks input/store byte purity.

Two actual pre-market histories from the fixed grid have the same baseline and
pending witness. One never commits cancellation; the other commits cancellation
of seven units. Both have a complete empty fill stream. Retaining their raw
before-restore histories reconstructs **0 versus 7 cancelled units**, with
**12 versus 5 executable units** before restoration. Logically restoring the
second authority observation to baseline yields equal authority/witness/fill
observations. Complete independent **fill-only** evidence therefore cannot
settle the cancellation. The experiment labels this logical evidence restore;
it does not claim another physical file-restore test.

If the archive and its independently supplied cut also jointly revert, both
worlds present identical classifier inputs and receive the same conditional
`certified_no_commit`. It is wrong about the committed history because the
independence/freshness assumptions failed. This is an impossibility witness,
not successful rollback detection. Independently retaining the full command
archive instead identifies historical commitment while reporting current
authority as a prefix behind the cut; it still authorizes no repair.

## Demonstrated defect, correction and independent review

Prototype `f1442de234cf0b92d25b3ef341d655e94ca9653a` trusted the older raw auditor
to validate input meaning. Its arithmetic also balanced malformed negative
order quantities and bool-valued trade side/quantity. It incorrectly returned
a definite historical disposition for those malformed inputs, although it
still authorized no writes. This is an experimental validation defect; the
unchanged production v4 facade remained frozen throughout.

The independent regression was pinned unchanged at `292b08a` before repair.
Strict independent raw schemas now reject those malformed identities, field
types, quantities, timestamps and unsupported options before arithmetic.
`tools/reproduce_pending_evidence_defect.py` preserves the exact prototype and
corrected source bytes/hashes, unchanged test hash, subprocess logs and JUnit:
**three regressions fail before and pass after**. The separate review found no
remaining material issue within the declared evidence contract.

## Reproduction, preservation and limits

```sh
python -m pytest tests/integration/test_pending_intent_evidence.py tests/integration/test_pending_intent_independent.py -q
python -m tools.pending_intent_experiment experiments/pending-intent-evidence
python tools/reproduce_pending_evidence_defect.py
```

The two complete runs reproduce **25 canonical JSON artifacts** byte for byte:
24 raw cases and the negative-evidence collection. The manifest pins source,
tests, contract and artifact hashes. Against exact starting PR2 `a452915`, all
**184 earlier source, documentation, test and tool files are byte-identical**.
Historical reports, negative findings and before/after reproducers are retained.
CI runs this study, defect reproducer, full aggregate and every previous helper;
its additional archive saves raw cases, negative witnesses, source copies,
JUnit/logs, dependency versions and exact checkout identity.

No entry recovery, cancellation repair, live fencing, authentication,
exactly-once, power-loss, hostile-source, physical-incarnation or profitability
claim follows. Bounds are 64 archived transitions and 4 MiB input evidence,
plus the modeled account/event bounds. The independent auditor supports
client-ID cancellations and known baseline accounts; it abstains outside that
subset. Prior inspected synthetic periods remain inspected examples, not fresh
holdouts.

The next economic step remains blocked on legally available multi-session market
data with preserved inspection history, calibrated fees/latency/queue behavior,
and measured complete command/execution evidence in a genuinely retained fault
domain. This study supplies none of those inputs. The predefined research
question is answered here; no further recovery version is introduced.
