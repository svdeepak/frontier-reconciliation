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

## M4 baseline — real run (control)

**Approach:** one prompt, one call per case, both CSVs and the output schema in
a single general-purpose request. No tools, no verification pass, no retries.
The experimental control.
**Model:** `minimax/minimax-m2.7` via OpenRouter, temperature 0, 14 cases,
14 LLM calls, 822.1s, $0.280687.
**Hypothesis:** a capable model given both files and the schema will detect most
breaks, but will be unreliable on the parts of reconciliation that are
arithmetic and set membership rather than judgment.
**Measured result:** F1 **0.8182** (precision 0.8571, recall 0.7826),
cause accuracy 0.7778, evidence validity 1.0, clean-case FP 0, TP=18 FP=3 FN=5
over 23 planted breaks.
**Failure modes:**
1. *Row-set completeness on multi-row breaks* — `case_10` attached a spurious
   A-side row to each duplicate; `case_13` cited only `B5012D` of the duplicate
   pair `{B5012, B5012D}`. Under exact row-set identity, each costs an FP and
   an FN.
2. *Schema formatting* — `case_04` emitted `component_causes: null` instead of
   omitting the field. Both row sets were correct, but the output failed
   validation and was scored honestly as unscoreable: −2 TP from formatting.
3. *Cause attribution requiring arithmetic* — labelled FX rounding-mode
   divergence as `ROUNDING_DIFFERENCE` (`case_08` ×2); gave COMPOUND components
   `[AMOUNT_MISMATCH, FEE_MISMATCH]` instead of `[FEE_MISMATCH, FX_DIFFERENCE]`
   (`case_12`, `case_13`).
4. *Unbounded reasoning on the smallest signal* — `case_09` (two one-cent
   deltas at the tolerance boundary) consumed 100,806 reasoning tokens and
   420.34s: 51% of run wall clock, 87% of run cost, for a correct answer.
**What it did not do:** never hallucinated a row ID; no false positives on either
clean case; not fooled by equal aggregate totals on `case_13` — it found 2 of 3
real breaks there.
**What this motivates:** every failure above is mechanical, not a lapse of
judgment. Row-set construction, tolerance comparison, FX attribution and schema
shape are all deterministically decidable. That is the case for putting
arithmetic in code and reserving the model for genuine ambiguity.
**Decision:** Kept as the frozen control. Not modified or re-run after results
were seen.

## M5/M6 solution — real run

**Approach:** deterministic-first five-stage pipeline — normalize → candidate
matching → arithmetic cause classification → LLM interpretation of unsettled
candidates only → deterministic verifier as the output gate. Same model and
provider as the baseline, same committed datasets, same frozen scorer.
**Measured result:** F1 **1.0** (precision 1.0, recall 1.0), cause accuracy 1.0,
evidence validity 1.0, clean-case FP 0, TP=23 FP=0 FN=0.
**Delta vs baseline:** F1 **+0.1818**, precision +0.1429, recall +0.2174,
cause accuracy +0.2222. Runtime 822.1s → 13.9s. LLM calls 14 → 1.
LLM cost $0.280687 → $0.00088047.
**Where the model was used:** 1 of 14 cases (`case_11_ambiguous` — two identical
amounts on the same date with references reading `REF ILLEGIBLE` and
`REF MISSING`). The other 13 were resolved deterministically.
**Verifier corrections:** 0. The deterministic stages supplied every amount and
the single LLM reply stayed within its remit, so the verifier had nothing to
correct on this run. Its value is evidenced by regression tests that feed it a
hostile reply (fabricated row IDs, wrong arithmetic, unsupported COMPOUND set)
and assert the scored output is unchanged.
**Disclosure:** this is a comparison of *architectures*, not of two LLM
configurations. The baseline makes 14 LLM calls and the solution 1, so the
runtime and cost deltas largely measure not calling the model. `1 of 14` is a
measured property of this evaluation set's ambiguity density, not a universal
property of the design.
**Limitations:** 14 cases, 23 breaks, one seed, one model, one run each — no
variance estimate. The matcher's reference normalization was written against
this generator's noise styles, so its near-total matching coverage is partly by
construction; F1 = 1.0 evidences correct conventions and plumbing rather than
demonstrated generalization.
**Decision:** Kept.

## Provenance fix — evaluator reads run metadata

**What:** `results/*.json` took `model`/`provider` from the first case output's
`system` block, which passes through the model's reply. In the real baseline run
the model misreported its own identity in all 14 files, claiming 8 distinct
model ids across 3 providers (`gpt-4o`, `gpt-4`, `o4-mini`,
`claude-3-5-sonnet-20241022`, `reconciliation-engine`, `reconciliation-core`,
`reconciliation-v1`, `recon-v1`) while actually running `minimax/minimax-m2.7`.
`results/baseline.json` therefore reported `gpt-4o` / `openai`.
**Why:** `baseline.py` spreads the model-supplied `system` block last, so
invented values overwrite the runner's true ones.
**Fix:** `evaluate.py` `load_run_provenance()` reads provider/model from the
runner-written `_meta.json`, falling back to case files only when absent; the
discarded self-report is preserved under `system_self_reported` for audit.
Scoring functions untouched; all metrics byte-for-byte identical (verified by
SHA-256 of `totals` and `per_case` before and after).
**Not changed:** `baseline.py`'s spread order and the 14 committed baseline
outputs — the baseline is the frozen control. The defect remains latent for any
future baseline run; documented in `docs/BASELINE_RUN_NOTES.md` §1.
**Decision:** Kept.

## Submission hardening — documentation

**What:** README completed with real measured results, scope/limitations, and a
hot take resting on measured data; `docs/PER_CASE_COMPARISON.md` generated from
the evaluator results; `docs/BASELINE_RUN_NOTES.md` completed with both runs;
`docs/SUBMISSION_CHECKLIST.md` updated to verified state;
`docs/DEMO_SCRIPT.md` drafted. XLSX reports regenerated from the **real**
solution outputs (previously MockProvider artifacts).
**Why:** every metric in the submission must be traceable to `results/*.json` or
`outputs/*/_meta.json`, and no mock result may read as a real one.
**Result:** zero metric placeholders remain; no document claims a model other
than `minimax/minimax-m2.7` for the real runs. No implementation, dataset,
schema, evaluator-semantics or baseline change.
**Open:** `.gitignore` still excludes `results/*.json`, `outputs/solution/` and
`reports/`, so no real run evidence is committable yet — flagged for decision in
`docs/SUBMISSION_CHECKLIST.md` §10, deliberately not changed unilaterally.
**Decision:** Kept.

## HELD-OUT GENERALIZATION EXPERIMENT — seed 777 (secondary; primary seed-42 result unchanged)

**Purpose:** test whether agent-v1 generalizes beyond the seed-42 dataset it was
developed against. Not an optimization pass — no implementation was changed
before, during, or after this experiment, and no result below was used to tune
anything.

**Setup:** `data-holdout/` generated with the existing generator at seed 777
(generator v1.0, unmodified). Same model and provider as the primary experiment
(OpenRouter / `minimax/minimax-m2.7`), same frozen evaluator, same schemas.
Separate output and result paths throughout; primary artifacts untouched.

**Measured results (seed 777, 14 cases, 23 planted breaks):**

| Metric | Holdout baseline | Holdout agent-v1 |
|---|---|---|
| F1 | 0.72 | **1.0** |
| Precision | 0.6667 | 1.0 |
| Recall | 0.7826 | 1.0 |
| Cause accuracy | 0.7778 | 1.0 |
| Evidence validity | 1.0 | 1.0 |
| TP / FP / FN | 18 / 9 / 5 | 23 / 0 / 0 |
| Predictions | 27 | 23 |
| LLM calls | 14 | 1 |
| Runtime | 566.97s | 12.56s |
| Prompt / completion tokens | 17,644 / 30,970 | 693 / 618 |
| Cost | $0.056913 | $0.001899 |

**Seed-42 vs seed-777:**

| | Baseline 42 | Baseline 777 | agent-v1 42 | agent-v1 777 |
|---|---|---|---|---|
| F1 | 0.8182 | **0.72** | 1.0 | **1.0** |
| Precision | 0.8571 | **0.6667** | 1.0 | 1.0 |
| Recall | 0.7826 | 0.7826 | 1.0 | 1.0 |
| FP | 3 | **9** | 0 | 0 |

**Finding 1 — agent-v1 held.** F1 1.0 on unseen values, 23/23 breaks, no false
positives, and the same single LLM call (`case_11_ambiguous`), 0 verifier
corrections. The deterministic stages transferred without modification.

**Finding 2 — the baseline degraded.** F1 0.8182 → 0.72, entirely through
precision (0.8571 → 0.6667): false positives tripled from 3 to 9 while TP, FN,
recall and cause accuracy were unchanged. On holdout `case_13` it emitted **9
predictions for 3 breaks**, six of them spurious `AMBIGUOUS` claims over cleanly
matched pairs, plus one duplicate prediction. Same prompt, same model, same
structure, different values — the one-prompt approach was less stable than its
seed-42 score suggested.

**Finding 3 — a metric that flatters the baseline.** On the holdout's *clean*
`case_02`, the baseline emitted **6 breaks** using an invented break type
`DATE_MISMATCH`, which failed schema validation. Because
`clean_case_false_positives` counts scored FPs and an invalid output contributes
none, that metric reads **0** for both seeds while the baseline in fact raised
six false exceptions on a clean case. The headline number understates the real
behaviour. `case_04` was also schema-invalid (`component_causes: null`, the same
slip as seed-42), costing 2 TP.

**Finding 4 — the holdout is narrower than "held out" suggests.** Structurally
it is a **replica** of seed-42: identical case list, identical row counts,
identical break-type composition, identical 23-break total, identical
net-totals-equal pattern (5 cases). Only values vary — 97% of the 334 rows
differ in reference and amount. But 8 rows are hardcoded in the generator, so
`case_12_compound` and most of `case_13_signature_adversarial` are byte-identical
across seeds, and the ground-truth breaks for `case_11`, `case_12` and `case_13`
are identical apart from the seed field. The ambiguous-case reference literals
(`REF ILLEGIBLE`, `REF MISSING`) are also fixed.

**What this experiment does and does not establish.** It shows agent-v1 is
robust to *value* variation — new amounts, dates, references, currencies — under
the same structure, and that the baseline is not. It does **not** test
generalization to new break structures, unseen reference-noise styles, different
case shapes, many-to-one settlement batches, or real partner data. Because the
matcher's reference normalization was written against this generator's noise
styles, and the generator is shared by both seeds, the holdout cannot detect
overfitting to the generator itself. A stronger test needs a different generator
or real-world files.

**Decision:** Recorded as a secondary result. No implementation change.
The primary seed-42 experiment (commit 1fc5850) stands unmodified.
