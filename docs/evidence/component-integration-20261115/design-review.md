# Independent design and assembly review

These are separate assistant reviewers, not external human review. Reviews were
completed before the six-case primary experiment. No reviewer evaluated engine
outcomes during design review.

`independent_feasibility` conditionally selected the composition study after
static inspection of prior evidence and the Exchange → Portfolio → Risk path.
It approved the committed fixed tape, literal fees, risk-only ablation and
rational oracle contract. Its requested pre-execution clarifications were:
numeric worker/stage caps, no overwrite/retry, and a post-fill quote-gate check
strictly before the common session cutoff. These were committed before execution.

`source_adversary` independently checked the event/book/queue/scheduler semantics:
initial snapshots need no RESET, initial same-time market delivery precedes the
strategy wake, the historical ADD joins behind the own bid, the trade restores
the historical 99/101 book before accounting, the remaining ASK can be canceled
after the configured delay, and no-kill inventory limits permit a replacement
bid. It identified the existing duplicate risk/strategy cancellation path; the
contract permits and records both requests rather than changing that behavior.

`publication_integrity` independently reconstructed the source union in memory:
14 changed source files equal their single pinned donor, metrics.py equals the
PR5-then-PR7 patch union, and the other 23 files equal main. All nine patch hashes
match, and the C0 manifest differs only at risk.py. The metrics union hash is
`4091dbedd3779aac776e27e8e5a12a42fd6a122e7307f3693b32e4021998c22a`.

The dependency audit verified 15 test paths and eight JSON inputs (298,134 bytes)
against donor Git objects and recorded hashes. Four existing fictional adapter
CSVs remain main-identical; no historical result archives were copied. Main and
known local component/stopped refs were unchanged. A separate authorized Git
`ls-remote` read on November 15 confirmed main and all twelve remote draft branch
heads remained at their supplied pins; the new study branch was not yet remote.

The composition is an integration candidate, not a proposal to merge or a finding
of economic advantage. Component-local exactness does not establish cross-system
rollback, cancellation liveness, passive fill realism, throughput or bounded
rational/output growth. Actual source/trace/artifact review follows separately.
