# HELD-OUT GENERALIZATION EXPERIMENT — seed 777

**This is a SECONDARY result. The primary experiment is seed-42, frozen at
commit `1fc5850`, and is unchanged by this document.**

Purpose: test whether agent-v1 generalizes beyond the seed-42 dataset it was
developed against. No implementation was modified before, during or after this
experiment; no result here was used to tune anything.

| | Primary | Held-out |
|---|---|---|
| Seed | 42 | **777** |
| Data | `data/` | `data-holdout/` |
| Baseline outputs | `outputs/baseline/` | `outputs/holdout-baseline/` |
| Agent outputs | `outputs/solution/` | `outputs/holdout-solution/` |
| Results | `results/baseline.json`, `results/solution.json` | `results/holdout-baseline.json`, `results/holdout-solution.json` |

Same model and provider throughout: OpenRouter / `minimax/minimax-m2.7`. Same
frozen evaluator and schemas. Same unmodified generator (v1.0).

## Reproduction

```bash
python -m recon.generate --seed 777 --out data-holdout

LLM_PROVIDER=openrouter OPENROUTER_MODEL=minimax/minimax-m2.7 \
  python -m recon.baseline --data data-holdout --outputs outputs/holdout-baseline
LLM_PROVIDER=openrouter OPENROUTER_MODEL=minimax/minimax-m2.7 \
  python -m recon.agent    --data data-holdout --outputs outputs/holdout-solution

python -m recon.evaluate --outputs outputs/holdout-baseline --data data-holdout \
  --out results/holdout-baseline.json
python -m recon.evaluate --outputs outputs/holdout-solution --data data-holdout \
  --out results/holdout-solution.json
python -m recon.evaluate --compare results/holdout-baseline.json \
  results/holdout-solution.json --out results/holdout-comparison.json
```

## Results (seed 777, 14 cases, 23 planted breaks)

| Metric | Baseline | agent-v1 |
|---|---|---|
| **F1** | 0.72 | **1.0** |
| Precision | 0.6667 | 1.0 |
| Recall | 0.7826 | 1.0 |
| Cause accuracy (over TPs) | 0.7778 | 1.0 |
| Evidence validity | 1.0 | 1.0 |
| Clean-case FP (as scored) | 0 | 0 |
| TP / FP / FN | 18 / 9 / 5 | 23 / 0 / 0 |
| Predictions | 27 | 23 |
| LLM calls | 14 | 1 |
| Runtime | 566.97s | 12.56s |
| Prompt / completion tokens | 17,644 / 30,970 | 693 / 618 |
| Cost | $0.056913 | $0.001899 |
| Verifier corrections | n/a | 0 |

## Seed-42 vs seed-777

| Metric | Baseline 42 | Baseline 777 | Δ | agent 42 | agent 777 | Δ |
|---|---|---|---|---|---|---|
| F1 | 0.8182 | **0.72** | **−0.0982** | 1.0 | 1.0 | 0 |
| Precision | 0.8571 | **0.6667** | **−0.1904** | 1.0 | 1.0 | 0 |
| Recall | 0.7826 | 0.7826 | 0 | 1.0 | 1.0 | 0 |
| Cause accuracy | 0.7778 | 0.7778 | 0 | 1.0 | 1.0 | 0 |
| TP / FP / FN | 18/3/5 | 18/**9**/5 | FP ×3 | 23/0/0 | 23/0/0 | 0 |
| LLM calls | 14 | 14 | 0 | 1 | 1 | 0 |
| Runtime | 822.1s | 566.97s | −255s | 13.88s | 12.56s | −1.3s |
| Cost | $0.280687 | $0.056913 | −80% | $0.000880 | $0.001899 | +$0.001 |

Baseline runtime and cost fell between seeds because seed-42's `case_09` burned
100,806 reasoning tokens (420s); no holdout case spiralled that way — the worst
was `case_13` at 140.44s. That variance is itself a property of the one-prompt
approach, not a change we made.

## Findings

**1. agent-v1 held at F1 1.0.** 23/23 breaks, zero false positives, same single
LLM call (`case_11_ambiguous`), zero verifier corrections. The deterministic
stages transferred to unseen values without modification.

**2. The baseline degraded — precision only.** F1 0.8182 → 0.72. TP, FN, recall
and cause accuracy were identical across seeds; false positives tripled (3 → 9).
On holdout `case_13` it produced **9 predictions for 3 ground-truth breaks**: the
compound and the missing row (2 TP), a partial duplicate, six spurious
`AMBIGUOUS` claims over cleanly matched pairs, and one duplicate prediction. Same
prompt, same model, same structure — only the values changed.

**3. `clean_case_false_positives` understates the baseline.** On the holdout's
clean `case_02`, the baseline emitted **6 breaks** with an invented break type
`DATE_MISMATCH`. That failed schema validation, and because the metric counts
scored FPs — which an invalid output cannot contribute — it reads **0** on both
seeds. The baseline did raise six false exceptions on a clean case. Read the
metric with this caveat; the scorer is behaving as specified, but the specified
metric is blind to this failure shape.

**4. Schema-invalid outputs recurred.** `case_04` again emitted
`component_causes: null` (identical to seed-42), costing 2 TP with both row sets
correct. Two of 14 holdout baseline outputs were schema-invalid vs one of 14 on
seed-42.

## Limitations — what "held out" does and does not mean here

The holdout is a **structural replica** of seed-42, not an independent dataset:

- Identical case list, row counts, break-type composition, 23-break total, and
  net-totals-equal pattern (5 cases).
- Values do genuinely vary: **97% of the 334 rows** differ in reference and
  amount, and every case's ground truth differs in at least the seed field.
- But **8 rows are hardcoded** in the generator. `case_12_compound` and most of
  `case_13_signature_adversarial` are byte-identical across seeds, and the
  ground-truth breaks for `case_11`, `case_12` and `case_13` are identical apart
  from the seed field. The ambiguous-case reference literals (`REF ILLEGIBLE`,
  `REF MISSING`) are fixed, which is why the LLM-routing behaviour is identical.

So this experiment establishes robustness to **value variation under fixed
structure**, and shows the baseline lacks it. It does **not** test new break
structures, unseen reference-noise styles, different case shapes, many-to-one
settlement batches, or real partner data. Critically, because both seeds share
one generator — and the matcher's reference normalization was written against
that generator's noise styles — **this holdout cannot detect overfitting to the
generator itself.** A stronger test requires a different generator or real files.

agent-v1's two seed-777 1.0s are therefore consistent with genuine robustness,
but they are two runs on one structure from one generator. Treat them as
supporting evidence, not proof of general reconciliation accuracy.
