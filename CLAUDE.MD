## CURRENT STATE (2026-08-31 — do not redo completed work)
- M1 evaluation contract: docs/EVALUATION.md, schemas/, taxonomy — FROZEN, approved
- M2: deterministic generator, 14 committed datasets (seed 42), signature
  adversarial case with equal aggregates + 3 row-level breaks — approved
- M3: deterministic evaluator (exact row-set identity, six-way diagnostics),
  MockProvider — approved, 33 tests passing
- M4: baseline runner + OpenRouter/Ollama providers implemented; REAL RUN PENDING

## NEXT TASK (only this, then STOP for review)
1. make test (expect 33 passed)
2. Real baseline: LLM_PROVIDER=openrouter, key/model from .env,
   make baseline && make eval
3. Commit outputs/baseline/ and results/baseline.json UNTOUCHED
4. Report: aggregate metrics; per-case TP/FP/FN; exactly what the baseline
   predicted on case_13_signature_adversarial vs ground truth — did aggregate
   equality cause missed row-level breaks?
5. Draft first CHANGELOG.md baseline entry (approach, model, hypothesis,
   measured result, failure modes, what they motivate for M5)
6. STOP. Do not start M5. Do not optimize the baseline after seeing results.

## STANDING RULES
- Contract frozen; baseline is the control — never retouch after results
- Never fabricate results, history, or timestamps; changelog updates live
- Small meaningful commits; milestone boundaries = human review gates
