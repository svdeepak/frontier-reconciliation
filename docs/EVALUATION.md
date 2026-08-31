# Evaluation Contract (v1.0)

**Status: FROZEN before any agent implementation.** Baseline and final solution
are evaluated against this contract, on the same committed datasets, with the
same deterministic scorer. Changes to this contract after baseline results are
recorded must be logged in CHANGELOG.md with rationale.

## 1. Task

Given two transaction sources (Source A: internal ledger, Source B: partner
statement), produce a reconciliation result that identifies, for each planted
discrepancy ("break"): the involved row IDs from each source, the cause, the
relevant amounts and difference, supporting evidence, a suggested action, and
a confidence level. Matched rows are reported as matched.

## 2. Break taxonomy (schema_version 1.0)

| Code | Meaning | Is a break? |
|---|---|---|
| MATCH | Rows correspond with no discrepancy | No |
| MISSING_IN_A | Present in B, absent in A | Yes |
| MISSING_IN_B | Present in A, absent in B | Yes |
| AMOUNT_MISMATCH | Gross amounts differ beyond rounding tolerance | Yes |
| FEE_MISMATCH | Fee component differs | Yes |
| FX_DIFFERENCE | Difference attributable to FX conversion (rates supplied) | Yes |
| ROUNDING_DIFFERENCE | Difference within rounding tolerance (<= 0.01 units per row) | Yes |
| DUPLICATE | Same economic transaction appears more than once in a source | Yes |
| AMBIGUOUS | Correspondence cannot be established with certainty from data | Yes |
| COMPOUND | Two or more of the above causes on the same correspondence | Yes |

Taxonomy is authoritative in `src/recon/taxonomy.py`; the JSON Schemas embed
the same enum and a test asserts they stay in sync.

## 3. Evaluation cases

Target: **12–15 independent dataset pairs** ("cases"). A case = one Source A
file + one Source B file + one ground-truth file, generated deterministically
from an explicit seed. A case may contain multiple planted breaks.

Planned composition (exact counts fixed in M2, recorded in the case manifest):

- 1–2 clean cases (all MATCH — false-positive pressure)
- 2 missing/extra cases (MISSING_IN_A / MISSING_IN_B)
- 2–3 amount/fee mismatch cases
- 2 FX/rounding cases
- 2 duplicate cases
- 1–2 ambiguous cases
- 1 compound case
- 1 **signature adversarial case**: one transaction carries both an FX/rounding
  difference and a fee mismatch, plus a duplicate/offsetting transaction such
  that aggregate totals net to zero while row-level breaks exist.

Baseline and final solution run on **exactly the same committed datasets and
ground truth**. Any resource difference between baseline and final is disclosed.

## 4. Scoring semantics

Let ground truth define a set of breaks G, and the system output a set of
predicted breaks P.

**Row-set identity.** A predicted break p is a candidate match for ground-truth
break g iff `set(p.a_row_ids) == set(g.a_row_ids)` and
`set(p.b_row_ids) == set(g.b_row_ids)`. Each g may be matched by at most one p
and vice versa.

- **True positive (TP):** predicted break with row-set identity to some g.
- **False positive (FP):** predicted break matching no g.
- **False negative (FN):** ground-truth break matched by no p.
- **Cause accuracy:** computed over TPs only. Correct iff predicted cause ==
  ground-truth cause. For COMPOUND, the full set of component causes must be
  identified (no partial credit for cause; detection credit is unaffected).
- **Evidence validity:** a predicted break is evidence-valid iff every row ID
  it references exists in the source data. Reported as a rate over P.
  Hallucinated row IDs also count the prediction as FP regardless of other
  fields.
- Detection credit does not require correct cause; cause is scored separately.

## 5. Metrics

Primary metric:

- **Break-detection F1** = 2·(precision·recall)/(precision+recall), where
  precision = TP/(TP+FP) and recall = TP/(TP+FN), aggregated over all cases
  (micro-averaged: pool TP/FP/FN across cases).

Secondary metrics (all reported for baseline and final):

- Break-detection precision and recall (components of F1)
- Cause classification accuracy (over TPs)
- Evidence validity rate (over P)
- False positives on clean cases (count)
- Runtime per case (wall clock, seconds)
- Model/API cost per full evaluation run (USD)

The evaluator is deterministic, requires no LLM judge, and runs with
MockProvider without credentials. Outputs: `results/baseline.json`,
`results/solution.json`, `results/comparison.json`.

## 6. Flagged ambiguity: "critical break recall"

The competition brief (§9) proposes "CRITICAL BREAK RECALL" as primary metric
but **does not define "critical."** The competition rubric and hackathon PDF do
not define it either. We do not invent severity semantics not present in the
source material. Instead:

- Primary metric is **row-level break-detection F1** as defined in §5, which is
  fully determined by the committed ground truth and requires no undefined
  severity judgment.
- Recall — the substance of the brief's proposal — is reported explicitly as a
  named secondary metric for every run.
- If a definition of "critical" is later supplied by the organizers, a severity
  field can be added to ground truth and critical-break recall computed without
  changing any other part of this contract.

## 7. Determinism and parity rules

- Data generation: explicit seed, pinned dependency versions, schema_version
  stamped in every file; generated datasets committed to the repo.
- Same cases, same ground truth, same scorer for baseline and final.
- No test-case editing after baseline results are recorded (additions allowed
  only as a new, clearly-labelled case set scored separately).
