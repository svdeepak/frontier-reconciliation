# Per-case comparison — baseline vs solution

Generated from `results/baseline.json`, `results/solution.json` and the two
`_meta.json` run records. Every number is read from those files; none is
estimated. Both systems ran on the same committed datasets with the same
deterministic scorer.

- **Baseline** `baseline-v1` — openrouter / `minimax/minimax-m2.7`, 14 LLM calls
- **Solution** `agent-v1` — openrouter / `minimax/minimax-m2.7`, 1 LLM call

`LLM?` is whether the *solution* pipeline called the model for that case; the
baseline called it once per case, for all 14.

| Case | GT breaks | Baseline TP/FP/FN | Solution TP/FP/FN | LLM? | Baseline s | Solution s |
|---|---|---|---|---|---|---|
| `case_01_clean_small` | 0 | 0/0/0 | 0/0/0 | no | 21.88 | 0.00 |
| `case_02_clean_messy` | 0 | 0/0/0 | 0/0/0 | no | 24.51 | 0.00 |
| `case_03_missing_in_b` | 2 | 2/0/0 | 2/0/0 | no | 15.64 | 0.00 |
| `case_04_missing_in_a` ⚠ | 2 | 0/0/2 | 2/0/0 | no | 24.38 | 0.00 |
| `case_05_amount_mismatch` | 2 | 2/0/0 | 2/0/0 | no | 35.65 | 0.00 |
| `case_06_fee_mismatch` | 2 | 2/0/0 | 2/0/0 | no | 30.77 | 0.00 |
| `case_07_amount_and_fee` | 2 | 2/0/0 | 2/0/0 | no | 55.01 | 0.00 |
| `case_08_fx_difference` | 2 | 2/0/0 | 2/0/0 | no | 28.40 | 0.00 |
| `case_09_rounding` | 2 | 2/0/0 | 2/0/0 | no | 420.34 | 0.00 |
| `case_10_duplicates` ⚠ | 2 | 0/2/2 | 2/0/0 | no | 20.82 | 0.00 |
| `case_11_ambiguous` | 1 | 1/0/0 | 1/0/0 | **yes** | 27.29 | 13.88 |
| `case_12_compound` | 1 | 1/0/0 | 1/0/0 | no | 16.58 | 0.00 |
| `case_13_signature_adversarial` ⚠ | 3 | 2/1/1 | 3/0/0 | no | 59.27 | 0.00 |
| `case_14_aggregate_trap` | 2 | 2/0/0 | 2/0/0 | no | 41.52 | 0.00 |
| **TOTAL** | **23** | **18/3/5** | **23/0/0** | 1 of 14 | **822.1** | **13.9** |

⚠ marks the five cases where the two systems diverge.

## Where the baseline lost points

| Case | What happened | Cost |
|---|---|---|
| `case_04_missing_in_a` | Both breaks had the correct row sets, but the reply set `component_causes: null` instead of omitting it, so the output failed schema validation and the evaluator scored it honestly as unscoreable. | −2 TP, +2 FN |
| `case_10_duplicates` | Added a spurious A-side row to each duplicate: predicted `A=[A1011], B=[B5011,B5011D]` where ground truth is `A=[], B=[B5011,B5011D]`. Exact row-set identity fails. | −2 TP, +2 FP, +2 FN |
| `case_13_signature_adversarial` | Reported the duplicate as `B=[B5012D]` only, missing its pair `B5012`. Half a duplicate is not a duplicate. | −1 TP, +1 FP, +1 FN |
| `case_08_fx_difference` (×2) | Detected both breaks but labelled them `ROUNDING_DIFFERENCE` instead of `FX_DIFFERENCE` — the one-cent gap is explained by rounding-mode divergence on an identical foreign amount and rate. | cause only |
| `case_12_compound`, `case_13` | Correct `COMPOUND` detection, wrong components: `[AMOUNT_MISMATCH, FEE_MISMATCH]` instead of `[FEE_MISMATCH, FX_DIFFERENCE]`. | cause only |

The pattern: the baseline is competent at spotting *that* something is wrong and
weakest at **row-set precision on multi-row breaks** (duplicates) and at
**attributing a cause that requires arithmetic** (FX vs rounding). Both are
exactly what the deterministic stages settle by construction.

## Runtime and cost

| | Baseline | Solution |
|---|---|---|
| Wall clock | 822.1s | 13.9s |
| LLM calls | 14 | 1 |
| LLM cost (USD) | $0.280687 | $0.00088047 |

**Read these with the disclosure below, not as a like-for-like model comparison.**
The baseline sends every case to the model; the solution sends one. The runtime and
cost gap therefore measures *architecture* — a deterministic-first pipeline versus a
one-prompt LLM — not two comparable LLM configurations. Baseline runtime is also
dominated by a single outlier case (`case_09_rounding`, 420.34s, 51% of the run);
see `docs/BASELINE_RUN_NOTES.md` §2.

