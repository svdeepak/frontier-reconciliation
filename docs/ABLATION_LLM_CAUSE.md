# ABLATION — LLM cause classification

**SECONDARY EXPERIMENT. The primary agent-v1 result (seed-42, F1 1.0, commit
`1fc5850`) is unchanged by this document and was not rewritten.**

## Hypothesis

Agent-v1 decides break causes by arithmetic and calls the LLM only for
candidates it cannot settle. If the LLM is at least as good at cause
classification as the arithmetic, replacing the deterministic classifier with
the LLM should leave accuracy unchanged and cost only extra latency and tokens.
If it is worse, the deterministic layer is doing measurable work.

Stated before the run, not after: the prediction was that accuracy would fall on
FX-versus-rounding attribution, because that is precisely where the one-prompt
baseline erred on seed-42 (`case_08` ×2, `case_12`, `case_13`).

## Exact control vs ablation difference

| | Control (agent-v1) | Ablation |
|---|---|---|
| Module | `src/recon/agent.py` | `src/recon/ablation_llm_cause.py` |
| Cause of a candidate break | **deterministic arithmetic** (`classify_pair`) | **LLM** |
| LLM invoked for | `AMBIGUOUS` candidates only | **every** candidate |
| Everything else | — | identical, by reuse |

**Identical between the two arms**, reused rather than reimplemented:
normalization, matching and candidate construction (`agent.normalize`,
`agent.match_candidates`, `agent.build_candidates`); the LLM system prompt
(`agent.LLM_SYSTEM_PROMPT`); reply parsing (`agent.strip_fences`); the taxonomy
gate (`agent.ALLOWED_TYPES`); **the verifier, unchanged and still downstream**
(`agent.verify`); every computed amount (row sets and
`amount_a`/`amount_b`/`difference` remain deterministic — the LLM is never asked
for a number); the `matches` array (still decided by deterministic arithmetic, so
the ablation varies cause labelling only); the output schema, the evaluator and
its scoring semantics; the model and provider (`minimax/minimax-m2.7` via
OpenRouter); and the dataset (committed seed-42).

**`agent.py` was not modified.** The ablation is a separate module that imports
it, verified byte-identical to commit `1fc5850` by SHA-256. It is gated behind
`LLM_CAUSE_ABLATION=1` and cannot run as part of a normal invocation.

Two design decisions taken to keep the comparison honest:

1. **The LLM is not shown the deterministic answer.** `agent._candidate_brief`
   embeds `deterministic_break_type`, which would hand the model the label under
   test. The ablation builds its own brief that withholds it while keeping every
   other field, and supplies real structural context (row counts per side,
   presence in each source) plus FX fields in its place.
2. **No candidate is relabelled `AMBIGUOUS`.** `agent.apply_decisions` accepts an
   LLM relabel only for `AMBIGUOUS` candidates; rather than fabricate that type
   to slip past the gate, the ablation implements its own ~20-line
   `apply_cause_decisions`.

## Reproduction

```bash
LLM_CAUSE_ABLATION=1 LLM_PROVIDER=openrouter OPENROUTER_MODEL=minimax/minimax-m2.7 \
  python -m recon.ablation_llm_cause --data data --outputs outputs/ablation-llm-cause

python -m recon.evaluate --outputs outputs/ablation-llm-cause --data data \
  --out results/ablation-llm-cause.json
python -m recon.evaluate --compare results/solution.json results/ablation-llm-cause.json \
  --out results/ablation-comparison.json
```

## Measured results (seed-42, 14 cases, 23 planted breaks)

| Metric | Control agent-v1 | **Ablation** | Delta |
|---|---|---|---|
| **F1** | **1.0** | **0.9778** | **−0.0222** |
| Precision | 1.0 | 1.0 | 0 |
| Recall | 1.0 | 0.9565 | **−0.0435** |
| **Cause accuracy (over TPs)** | **1.0** | **0.8636** | **−0.1364** |
| Evidence validity | 1.0 | 1.0 | 0 |
| Clean-case FP | 0 | 0 | 0 |
| TP / FP / FN | 23 / 0 / 0 | **22 / 0 / 1** | −1 TP, +1 FN |
| Predictions | 23 | 22 | −1 |
| **Verifier corrections** | **0** | **4** | **+4** |

### Resource cost

| | Control | Ablation | Factor |
|---|---|---|---|
| LLM calls | 1 | 12 | **×12** |
| Runtime | 13.9s | 129.05s | **×9.3** |
| Prompt tokens | 689 | 11,523 | ×16.7 |
| Completion tokens | 643 | 8,015 | ×12.5 |
| Reasoning tokens | not recorded separately | 4,801 | — |
| Cost | $0.00088047 | $0.01309202 | **×14.9** |

(12 calls rather than 14 because the two clean cases produce no candidates, so
there is nothing to classify.)

### Agreement with the deterministic classifier

The LLM's label was accepted for **23 of 23** candidates (all within the
taxonomy). It **agreed with the arithmetic on 19 of 23 (82.6%)** and diverged on
four:

| Case | Deterministic | LLM | Outcome |
|---|---|---|---|
| `case_08_fx_difference` C-001 | `FX_DIFFERENCE` | `ROUNDING_DIFFERENCE` | cause error (TP kept) |
| `case_08_fx_difference` C-002 | `FX_DIFFERENCE` | `ROUNDING_DIFFERENCE` | cause error (TP kept) |
| `case_13_signature_adversarial` C-002 | `COMPOUND[FEE_MISMATCH, FX_DIFFERENCE]` | `ROUNDING_DIFFERENCE` | cause error + 3 arithmetic corrections |
| `case_12_compound` C-001 | `COMPOUND[FEE_MISMATCH, FX_DIFFERENCE]` | `COMPOUND` with prose components | **break DROPPED → the 1 FN** |

**All four divergences are the same underlying error**: failing to attribute a
one-cent gross gap to FX rounding-mode divergence when both rows carry an
identical foreign amount and rate. This is the exact failure the one-prompt
baseline made on seed-42, reproduced here even though the model was given the
computed difference and the FX fields explicitly.

### Two mechanisms worth recording

**The dropped break.** On `case_12` the model returned `component_causes` as
prose rather than bare enum values:

```
"FEE_MISMATCH: fee A 2.50 vs B 3.35 (delta -0.85) — unexplained discrepancy…"
"ROUNDING_DIFFERENCE: gross A 1340.30 vs B 1340.29 — same EUR 1234.50 @ 1.0857…"
```

The taxonomy filter stripped both (neither is a valid enum member), leaving an
empty component set, and the verifier dropped the break as
`compound_component_set_unsupported`. Recall fell from 1.0 to 0.9565 through a
formatting habit, not a reasoning failure — the model's *prose* was correct about
the rounding mode. Note this is the same `null`/malformed-field habit that cost
the baseline 2 TP on `case_04`.

**The verifier absorbed the rest.** On `case_13`, relabelling a COMPOUND as
`ROUNDING_DIFFERENCE` changed which amounts `_recompute()` reports (gross rather
than net), so the verifier issued three `CORRECTED_ARITHMETIC` entries and
restored the correct figures. The break survived as a TP with a wrong cause.
Verifier corrections rose 0 → 4, which is the deterministic safety net doing
visible work: **without it, the ablation's numeric output would have been wrong,
not merely mislabelled.**

## Kept or rejected

**REJECTED.** The ablation is worse on every accuracy axis that moved and costs
~15× more:

- F1 −0.0222, recall −0.0435, cause accuracy **−0.1364**
- one ground-truth break lost entirely
- 4 verifier interventions where the control needed none
- ×12 LLM calls, ×9.3 runtime, ×14.9 cost

agent-v1's deterministic cause-classification layer is retained unchanged. The
experiment measured a real cost to removing it, in both accuracy and resources.

**No implementation was tuned in response to this result**, and agent-v1's
default path was not touched. The one code change made mid-experiment was a
crash fix in the ablation harness itself: the first attempt died at `case_14`
because the model sent an explicit `"component_causes": null` and
`dict.get(k, [])` returns `None` for a present-but-null key. That was a bug in
this module, not a finding, and the run was restarted from scratch so all 14
cases come from one consistent run.

## Limitations

- **One run, one model, one dataset.** 14 cases, 23 breaks, seed 42,
  `minimax/minimax-m2.7`, no repeats — no variance estimate. A different model,
  or the same model on another day, could disagree differently.
- **The verifier confounds the accuracy reading.** Because the verifier was kept
  unchanged downstream (as specified), a wrong COMPOUND component set causes the
  break to be *dropped* rather than merely mislabelled. So part of the measured
  recall loss reflects verifier strictness, not classification accuracy alone.
  This affects the 2 COMPOUND breaks of 23. A variant that let wrong causes
  through would separate the two effects, and was not run.
- **Prompt not tuned for this task.** The ablation deliberately reuses
  `agent.LLM_SYSTEM_PROMPT` verbatim, which was written for interpreting
  ambiguous candidates rather than for exhaustive cause classification. A prompt
  written for this task might well do better. Tuning it would have made the
  ablation a different experiment — an optimization pass — which was explicitly
  out of scope.
- **Ablation-favourable setup in one respect:** the LLM received the
  deterministic *computed amounts and evidence string*, including the FX
  arithmetic. It still missed the FX attribution. A cause classifier working from
  raw rows alone would likely do worse, not better.
- The result speaks to this generator's break constructions. As with the primary
  and holdout experiments, generalization to real partner data is untested.
