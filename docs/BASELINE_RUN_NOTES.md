# Real run notes — baseline and solution

Recorded from the real runs. Facts only, read from `outputs/*/_meta.json` and
`results/*.json`. The baseline is the frozen experimental control: **nothing in
the baseline was changed in response to any observation below**, and the
baseline was not re-run after its results were seen.

| | Baseline | Solution |
|---|---|---|
| System | `baseline-v1` | `agent-v1` |
| Provider / model | `openrouter` / `minimax/minimax-m2.7` | `openrouter` / `minimax/minimax-m2.7` |
| Cases completed | 14/14 | 14/14 |
| LLM calls | 14 | 1 |
| Parse failures | 0 | 0 |
| Wall clock | 822.1s | 13.9s |
| Prompt tokens | 17,678 | 689 |
| Completion tokens | 124,984 | 643 |
| LLM cost | $0.280687 | $0.00088047 |
| Verifier corrections | n/a | 0 |

Both arms used the **same model and provider**, the same committed datasets, and
the same frozen scorer. They differ in architecture, and therefore in LLM usage
(14 calls vs 1) — see §4.

## 1. Provenance defect (fixed in the evaluator, not in the outputs)

`results/baseline.json` originally reported `model: gpt-4o, provider: openai`
while the run actually used `minimax/minimax-m2.7` on OpenRouter.

**Cause.** `baseline.py` builds each case's `system` block as
`{name, model, provider, **model_supplied_system}` — the model's own JSON is
spread *last*, so any `model` / `provider` it invented overwrote the runner's
true values. The evaluator then adopted the first case file's block as the
run's identity. The model fabricated **8 distinct model ids across 3 providers**
in the 14 case files:

| Reported model | Reported provider |
|---|---|
| gpt-4o, gpt-4, o4-mini | openai |
| claude-3-5-sonnet-20241022 | anthropic |
| reconciliation-engine, reconciliation-core, reconciliation-v1, recon-v1 | internal |

Not one case reported the real model or provider.

**Fix.** `evaluate.py` now takes run provenance from the runner-written
`_meta.json` (`load_run_provenance()`), falling back to the case files only if
absent. The discarded self-report is preserved under `system_self_reported` so
the misreport stays visible rather than being silently overwritten. Scoring
functions and all metrics are unchanged.

**Still latent in `baseline.py`.** The spread order was deliberately *not*
changed: the baseline runner is the frozen control, and no committed baseline
output was touched. Any future run of `baseline.py` will again record
model-supplied identities in its case files, and any consumer reading a case
file's `system` block rather than `_meta.json` will be misled. Worth fixing
before a second baseline run, as a disclosed change.

**Broader point.** A model asked to fill a schema field about itself will
confabulate a plausible value. Self-reported provenance is not evidence; only
runner-side metadata is.

## 2. case_09_rounding — runaway reasoning

| Metric | case_09 | Mean of other 13 | Ratio |
|---|---|---|---|
| Wall clock | 420.34s | 30.9s | 13.6× |
| Completion tokens | 101,296 | 1,822 | 55.6× |
| Reasoning tokens | 100,806 | 1,133 | 89.0× |
| Cost | $0.243862 | $0.002833 | 86.1× |

case_09 alone accounts for **51.1% of run wall clock, 81.0% of completion
tokens and 86.9% of run cost.** Reasoning tokens were 99.5% of its completion
output (100,806 of 101,296) — essentially all spend went to hidden reasoning,
not to the answer.

**What it was asked to do.** case_09 contains two `ROUNDING_DIFFERENCE` breaks,
the smallest signal in the dataset — a one-cent gross delta at the tolerance
boundary (`ROUNDING_TOLERANCE = 0.01`):

```
A1011 gross 353.57 / fee 3.54    B5011 gross 353.56 / fee 3.54   delta 0.01
A1012 gross 297.41 / fee 2.97    B5012 gross 297.40 / fee 2.97   delta 0.01
```

**What it produced.** A correct answer: 2 predictions, both exact-row-set TPs
with the right cause and difference 0.01, plus 10 correct matches. Scored
`tp=2 fp=0 fn=0`.

**Observation, not diagnosis.** The reasoning trace is not visible in the
response, so *why* it spiralled cannot be established from the recorded data.
What the data does support: the case with the smallest per-row signal, sitting
exactly on the tolerance boundary, consumed ~89× the reasoning of the mean case
and still arrived at the correct answer. A one-cent delta is precisely the
decision a `Decimal` comparison against a fixed tolerance settles in one
operation, and it is the kind of judgment where a reasoning model appears to
have no natural stopping condition.

Two further facts worth noting for analysis:

- `case_06_fee_mismatch` recorded **0 reasoning tokens** and still scored
  correctly, so reasoning effort varied enormously across structurally similar
  cases within a single run.
- No token ceiling was hit and no request failed; the 101,296 completion tokens
  were spent voluntarily.

**Implication for the comparison, to state rather than assume.** Baseline
runtime and cost are dominated by one outlier case. Any baseline-vs-solution
runtime or cost claim must either report the median alongside the total, or
disclose that a single case drove ~half the wall clock and ~87% of spend.
Reporting only the total would overstate the typical per-case baseline cost by
roughly an order of magnitude.

## 3. Accuracy summary

| Metric | Baseline | Solution |
|---|---|---|
| F1 (primary) | 0.8182 | 1.0 |
| Precision | 0.8571 | 1.0 |
| Recall | 0.7826 | 1.0 |
| Cause accuracy (over TPs) | 0.7778 | 1.0 |
| Evidence validity | 1.0 | 1.0 |
| Clean-case FP | 0 | 0 |
| TP / FP / FN | 18 / 3 / 5 | 23 / 0 / 0 |

Over 23 planted breaks in 14 cases. Per-case detail:
[PER_CASE_COMPARISON.md](PER_CASE_COMPARISON.md).

### Baseline failure modes (as scored)

- **Row-set completeness on multi-row breaks.** `case_10_duplicates`: attached a
  spurious A-side row to each duplicate (`A=[A1011], B=[B5011,B5011D]` vs truth
  `A=[], B=[B5011,B5011D]`). `case_13`: cited only `B5012D` for the duplicate
  `{B5012, B5012D}`. Under exact row-set identity each is an FP plus an FN.
- **Schema formatting.** `case_04_missing_in_a`: both row sets were correct, but
  the reply set `component_causes: null` instead of omitting the field. The
  output failed schema validation and was scored honestly as unscoreable —
  −2 TP, +2 FN from a formatting slip alone. It is the only schema-invalid
  output in the run.
- **Cause attribution requiring arithmetic.** `case_08` (×2): labelled
  `ROUNDING_DIFFERENCE` where the one-cent gap is FX rounding-mode divergence on
  an identical foreign amount and rate. `case_12` and `case_13`: correct
  `COMPOUND` detection with components `[AMOUNT_MISMATCH, FEE_MISMATCH]` instead
  of `[FEE_MISMATCH, FX_DIFFERENCE]`.

What the baseline did **not** do: it never hallucinated a row ID (evidence
validity 1.0) and never raised a false positive on either clean case. On the
signature adversarial case it was not fooled by the equal totals — it found 2 of
3 real breaks. Its losses were mechanical, not a failure of judgment.

## 4. Comparison scope — what these numbers do and do not show

The baseline sends all 14 cases to the model; the solution sends 1 and resolves
13 deterministically. So:

- The **accuracy** delta (F1 +0.1818) compares a deterministic-first pipeline
  against a one-prompt LLM on identical data — a fair architectural comparison,
  and the one the project set out to make.
- The **runtime** (−98.3%) and **cost** (−99.7%) deltas largely measure *not
  calling the model*. They are not a like-for-like comparison of two LLM
  configurations and must not be presented as one.
- The solution's accuracy is **overwhelmingly deterministic**: 13 of 14 cases
  never reached the model, and the one LLM call could not have altered any
  amount or row set.
- **`1 of 14` is a measured property of this evaluation set**, reflecting its
  ambiguity density (the generator plants exactly one unusable-reference case).
  It is not a universal property of the architecture.
- Baseline runtime and cost are **outlier-dominated** (§2): quote the median
  (~28s) alongside the 822.1s total.
- **Verifier corrections were 0** on this run — the deterministic stages supplied
  every amount and the single LLM reply stayed in its remit. The verifier's value
  is evidenced by regression tests that feed it a hostile reply, not by this
  run's counter.
- **Single run, single seed, single model.** No variance estimate. F1 = 1.0 means
  no scoring error on this set, not general reconciliation accuracy.
