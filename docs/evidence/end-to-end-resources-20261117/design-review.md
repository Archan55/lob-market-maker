# Independent pre-execution review

The owner's November 17 instruction explicitly permits routine modest workload
and cap choices for descriptive resource characterization, without requiring a
production workload target. This changes the November 16 feasibility decision.
It does not authorize reopening an optimizer, changing old gates or claiming
capacity/economic advantage.

`independent_feasibility` approved the distinct scope: unchanged PR13, 2,000 and
20,000 exposed seed-7 events, four fresh sequential workers, RSS/HWM and physical
output sizes across the whole application lifetime. It requested explicit raw
units, narrow normalization, the same-list input writer, pre-audit application
completion, nonoverlapping artifact audits and correct counter interpretation.
All were fixed in the design before application execution.

`performance_reproducer` and its independent static cross-check confirmed the
actual source boundaries and object lifetimes. Original integrated plotting
reloads tables while engine locals remain live; a separate post-return report
would change that workflow. The existing tracer starts after scheduler population
and ends before row/metric materialization, output and report generation.

`source_adversary` reviewed final harness SHA-256
`2b67807c11a75862175cd8b7cc719eaecbb3f512191f2a2187763e048db8b4c0`
and returned GO. Before any measurements, review corrected the actual plots/
paths, terminal output-cap check, preservation of the first stop cause, live
temporary-file disappearance handling and offline raw-counter/cap verification.
The original Scheduler.run and writer are each called once with unchanged
arguments, report import remains lazy, source selection is isolated, and
post-completion samples are excluded from headline peaks.

`publication_integrity` independently confirmed all 38 production files equal
PR13 and its frozen C1 manifest, with no missing/extra paths or production diff.
The original benchmark YAML and all earlier local/stopped refs were unchanged.
Its file-accounting review led to explicit non-atomic intermediate observations
and final-only reconstruction of cache/bookkeeping/block totals. Cross-filesystem
allocation-block equality is not assumed.

Focused preflight tests validate Linux counter parsing/units, missing-counter
handling, stage assignment, exact normalization, file classes, cap equality and
PNG decoding. They passed without importing the application or launching any
resource worker. No new RSS, application input, timing or output observations
were available during these design/source/harness corrections.

These are separate assistant reviews, not external human review. Actual sample,
physical-artifact and result review follows after the fixed observations.
