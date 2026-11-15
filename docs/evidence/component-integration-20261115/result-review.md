# Independent review of actual results

`growth_reproducer` audited the saved six primary runs with its own stdlib
Fraction calculations and PyArrow reads, without importing the application,
rerunning engines or editing evidence. It did not rely on runner pass flags.

The reviewer rehashed all 192 entries in the original full run manifest and
checked the 38-file source copies for each role and actual module import paths.
Only risk.py differs between C0 and C1. All 66 exact risk handoffs match the
independent rational ledger and their latest original snapshot encodings. The
reviewed observer captures same-object checks before delegating each original
call exactly once; JSON/Float64 records alone do not prove Python object identity.
Decimal text and tuple encodings agree, and all 33 C1 calls preserve caller
context attributes/traps/flags and Python's integer string safety guard.

Exact below-drawdown fee is `1 - 10^-29`, cash is `-101 + 10^-29` and net P&L is
`-1 + 10^-29`. Physical Float64 cash/P&L are `-101.0`/`-1.0`. C0 has seven
threshold-decision mismatches at `[20,20,20,30,30,40,40]`; its first call at 20
changes Inexact/Rounded flags. Other C0 controls and all C1 calls agree with the
exact risk oracle. C0's deliberate defects are the predefined negative control,
not new independent discoveries or unexpected study failures.

The actual 48 engine and six input Parquets were audited. All 36 ordered P&L rows
and 600 Decimal-to-float cells match their expected projections bitwise. Risk
timestamps are the complete fixed eleven-call vector per case, and P&L row
timestamps are `[0,10,20,20,30,40]`. Independently derived inventory exposure is
20/40 = 0.5 (the row fraction is 2/3), horizon 5 targets 25 and observes the first
state at 30 with quantity coverage 1; horizon 30 targets 50 and is censored with
coverage 0. No terminal markout is imputed.

At time 20 C0 below-drawdown suppresses its wake; C1 has two enabled wakes in
separate causal waves. Both inclusive-loss cases halt at 20 for max_loss and the
ordinary cases do not halt. Kill cases send cancellation of accepted ASK
`O000000000002` at 20, reach it at 25 with latency 5, then receive `cancelled`
followed by `cancel_rejected`. No new order is requested after those kills.

The reviewer's first scratch audit assumed one time-20 wake, which the contract
did not require. Inspection corrected that audit-only assumption to permit the
recorded causal waves. No study criterion, input, source, artifact or engine run
was changed or repeated because of it.

Numerical/artifact verdict: GO within the frozen scope. This supports synthetic
composition and wiring, not economics, performance, passive fill calibration,
cancellation liveness or exercising all nine components on this one tape.

`publication_integrity` independently returned GO after rehashing all 192 full
manifest entries and checking the six ordered attempts, seal, 38 source pins,
twelve before/after import snapshots, 633 raw trace records and the same physical
Parquets/projection cells. It confirmed every kill case's accepted ASK cancel at
25 and the quote-gate distinction at 20. All 127 publication copies match their
originals; the 76 omitted duplicate source files reconstruct exactly and no unique
result/provenance bytes are lost. Its correction from a mutable main label to the
immutable original SHA affects publication metadata only, not source or outcomes.
Remote CI/PR verification is recorded in the PR after publication, separately
from these local checks. Neither review constitutes external human review.
