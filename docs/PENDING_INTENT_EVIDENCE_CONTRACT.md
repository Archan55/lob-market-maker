# Pending cancellation classification: predefined read-only experiment

Fixed before new experiment outcomes, with independent design review on 2026-10-19. Source: Archan55/lob-market-maker PR2 a452915e940f70dc642b9a67d4874b79bdf09ba3; PR1 d4104b70660976c6d59fe8915a75ac5616bdecd3; main 1ce45d29081b1a16699cd1e31a4fb3b088ca0a2f.

## Blocking prerequisite and scope

The initial local gh metadata read returned POST https://api.github.com/graphql: Forbidden. A subsequent trusted parent note confirmed separately authenticated connector reads and authorized their use without credential changes. The connector then successfully reviewed repository metadata, both open draft PRs, empty PR comments/PR2 review threads and zero open issues. Normal Git transport verified all three tips. The local gh path was not retried. No AGENTS.md or .agents skills exist in the saved repository/environment. Work proceeds only on existing draft PR2; historical source/docs remain unchanged. No entry recovery, live connectivity, deployment or security change.

## Question and hypotheses

Can independently retained evidence classify a pending cancellation as historically committed or not committed at a closed cut while accepted orders keep filling autonomously?

H1: Under the explicit complete transition archive assumptions below, exact pending old/proposed digests can be related to historical prefix transitions even after later fills change the current full-document digest. A bounded read-only classifier can return committed-before-cut or not-committed-before-terminal-cut without changing either store.
H2: Independent complete fill-only evidence cannot settle a cancellation with no fill: no commit and commit-then-authority-rollback can have identical visible authority/witness/fill evidence.
H3: Classification alone changes neither cancellation liveness nor exposure. Pending v4 remains frozen; accepted orders fill through the underlying modeled venue after the failed caller releases its SQLite transaction. No cancellation after classification is performed.
H4: Missing, stale, contradictory, gapped, wrong-lineage or jointly rolled-back archive evidence cannot justify unconditional noncommit or safe recovery.

## Evidence assumptions fixed before results

A. One trusted exact quiescent baseline authority document, logical authority/witness/incarnation IDs, and exact pending intent old/proposed digests and watermark. A candidate capsule retains the exact raw account/command/token/cancel request, independently bound to both hashes. v4 itself stores no such capsule; this is experimental input, not an upgrade to its API.
B. A hypothetical independent source retains every committed authority transition (including commands with no fill, claims, no-ops and market-trade inputs) in one complete ordered prefix with immutable sequence and before/after state digests. It covers all accounts in the full-document hash. Evidence is retained across authority/client/witness restores. Production snapshot copies or hashes alone do not establish independence.
C. The source supplies a separately pinned fresh query cut with complete prefix head digest and sequence. To infer noncommit, the old pending writer is dead and cannot resume; no delayed transport command can still arrive after the cut. The simulator has local synchronous dispatch, with no delayed external sink. A complete fill stream alone does not satisfy command coverage.
D. The archive has no post-commit recording gap. This is a stated synthetic oracle assumption, not a constructed durable guarantee: a third SQLite append after a commit can die before recording it. Negative evidence tests remove this assumption. No hostile tampering, authentication, hash-collision, physical identity, power-loss or external account-universe claim.
E. Authority and archive rollback are separate modeled faults. If the independent cut and archive can jointly revert to the same old prefix, later effects become indistinguishable. The classifier is conditional; it must not claim to detect that fault using the restored evidence itself.

## Baselines and bounded cases

Baseline remains the unmodified cancellation-only v4 facade. Generate actual pending cancellations by injected fail-stop exceptions after intent and after authority commit; these release SQLite locks rather than model a live external venue running through an in-flight local lock. Existing real-SIGKILL evidence is tested separately; these new cases do not pretend to repeat OS process-death tests.

Fixed 24-case grid: bid/ask target side; cancel committed/uncommitted; initial target fill 0/2 units; autonomous later trade budget 0/1/7 units. Before takeover accept a seven-unit target and five-unit same-side sibling (prices 113/109 for bids, 127/131 for asks). Cancel only target, leaving sibling available for genuine post-cancel descendant fills. Initial fill occurs before pending baseline. After classifier evaluation, advance another seven-unit market budget independently of facade. Observe v4 cancellation still blocked. No repair or counterfactual cancellation.

Fixed negative cases: missing archive; stale cut; sequence gap; contradictory raw transition; wrong lineage; no terminal/dead-writer guarantee; fill-only indistinguishable pair; commit-to-observer crash gap; authority-only rollback with retained archive; jointly restored authority/witness/archive+cut indistinguishable pair. No new variants after evaluating these outcomes.

## Oracles, acceptance and stopping rules

Generate venue histories via public v3/v4 methods. Derive inventory, cash, fills, cancellations, executable residual and worst-case signed exposure directly from raw command/trade events with an independent integer FIFO auditor that does not import production fold/apply. Compare to public snapshots only after reconstruction. Store raw source inputs, actual SQLite documents and candidate transition. Classification performs zero writes; verify byte-identical stores before/after each evaluation.

Acceptance: all 24 complete cases classify the exact target transition correctly; all detectably invalid evidence returns unresolved; the jointly restored archive/cut pair returns the same conditional answer for indistinguishable inputs, demonstrating failed assumptions rather than impossible detection; autonomous fills continue after both pending crash boundaries; all quantity identities reconcile; classification changes no authority/witness bytes or cancellation capability; all negative indistinguishable pairs retain equal visible evidence despite different historical disposition/exposure. Repeated canonical JSON must be byte-identical. Any mismatch falsifies H1 and is retained rather than repaired in this run. Stop after those cases and independent review; do not introduce another protocol version or enable cancellation/entry recovery.

Report quantities and signed inventory/cash, gross executable quantity and long/short extrema (never net bid/ask executable risks). Cash is tick notional, not profit; synthetic uncalibrated outcomes say nothing about market profitability. Record full aggregate/local quality outcomes and blockers honestly. Use the authenticated connector for PR updates and exact-head CI verification; stop if an actual action is denied. No success claim until terminal verification.
