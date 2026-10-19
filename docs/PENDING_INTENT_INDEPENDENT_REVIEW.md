# Independent review: pending cancellation evidence

Two read-only design/adversarial reviewers worked in the saved repository while
the primary agent implemented the separate tools. One independently authored
`tests/integration/test_pending_intent_independent.py`; no reviewer changed the
classifier or production files. The contract preceded experiment outcomes.

The design review rejected fill-only completeness: a cancellation can commit
without a fill, and command commitment followed by authority restore can look
identical to no commitment. It required identity-bound raw candidate evidence,
historical prefix reconstruction, an independently fresh complete cut, and a
terminal pending writer before interpreting absence. It also corrected an
impossible acceptance criterion: jointly restored evidence and cut cannot be
required to abstain when identical valid-looking inputs receive a conditional
answer. Their failure must be retained as an explicit negative counterexample.

The adversarial review confirmed that pending v4 validation blocks trade,
snapshot and cancellation before authority state is inspected. It identified
the distinction between unfilled quantity and executable quantity after
cancellation. Classification cannot change that availability boundary, so the
matrix records actual continued fills and zero cancellation-liveness gain.

The independently authored 24-case accounting checks parse only raw order,
cancel and market inputs, with separate integer/FIFO state and closed formulas.
They verify fill/cancel/residual identities, both signed inventories/cash, live
quantity-time, attainable inventory intervals and unchanged input/store bytes.
They do not import production fold/apply or the primary accountant. Nine further
checks challenge missing/changed/nonterminal/fill-only/wrong-head evidence,
same-command/different-prefix history, and malformed field semantics.

Review found a real experimental defect: a rehashed archive containing bool
trade side or quantity, and a malformed current document containing negative
original quantity, received definitive classifications. Ordinary checksums do
not validate input meaning. All three regressions were committed unchanged at
`292b08a` before the independent raw-schema repair; the pinned prototype is
`f1442de234cf0b92d25b3ef341d655e94ca9653a`. The original test hash is
`9403a43949e53322212e4fca6036388118e549ffe3cd0f64e446e60592977f26`.

The unchanged regressions fail three times against the prototype and pass after
repair. Both complete new suites pass **58 tests**, including **33 independently
authored tests**. The repaired parser validates raw schemas before arithmetic;
the separate design reviewer reran its bool-valued quantity probe and obtained
`unresolved`. Neither version authorizes recovery; production source is unchanged.

No remaining material issue was found within the explicitly conditional bounded
contract. Trusted provenance, genuinely independent retention, archive
completeness, fresh external cut and dead-writer guarantees are assumed inputs,
not established by the local harness. Exact-transition noncommit is not proof
that the immutable command never executed before baseline. All pending/entry
recovery remains disabled and synthetic cash has no economic interpretation.
