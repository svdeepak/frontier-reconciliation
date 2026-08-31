# Improvement Changelog

Entries are recorded as work happens, never reconstructed retroactively.

## M2 — data model amendment (pre-data, pre-baseline)

**What:** Added two nullable fields to the transaction schema: `foreign_currency`,
`foreign_amount`, with the convention `gross_amount = round(foreign_amount × fx_rate, 2)`.
**Why:** M2 requirement that FX discrepancies be mathematically explicit and
reproducible rather than merely labelled. Required fields and scoring semantics
unchanged; contract §2–§5 untouched. No datasets or baseline results existed
prior to this amendment.
**Result:** FX_DIFFERENCE breaks are now constructed as rounding-mode divergence
(HALF_UP vs DOWN) on identical foreign amount and rate — verifiable by arithmetic.
**Decision:** Kept.
