# Final Submission Checklist

Status legend: `[x]` done and verified · `[ ]` outstanding · `[!]` blocked on a
real-provider run.

Nothing in this file may be marked `[x]` on intent alone — only on a verified
artifact in the repository.

## 1. README

- [x] Problem statement and target user
- [x] Architecture, five stages, module map
- [x] Deterministic / LLM boundary documented explicitly (§2.1)
- [x] Evaluation contract summarized, with link to the frozen document
- [x] Dataset table: 14 cases, 23 planted breaks, net-totals-equal flag
- [x] Signature adversarial case documented with committed-data arithmetic
- [x] Reproduction commands (`make data` / `baseline` / `solve` / `eval`)
- [x] Test summary (113 tests)
- [x] Repository layout
- [x] Model and tool disclosure
- [x] Results table — real measured values, zero placeholders remaining
- [x] Runtime model ID recorded (`minimax/minimax-m2.7` / OpenRouter)
- [x] Hot take (§11) — written from measured results
- [x] Scope/limitations section (§6.3) disclosing 14 cases, single run, 14-vs-1
      LLM calls, dataset-overfit risk, outlier-dominated baseline runtime

## 2. Results

- [x] `results/baseline.json` — real run, F1 0.8182, provenance correct
- [x] `results/solution.json` — real run, F1 1.0, provenance correct
- [x] `results/comparison.json` — primary delta +0.1818
- [x] All ten README placeholders replaced with measured values
- [x] Per-case TP/FP/FN table — `docs/PER_CASE_COMPARISON.md`, generated from
      the evaluator results
- [x] Baseline's prediction on `case_13_signature_adversarial` vs ground truth:
      found 2 of 3; missed the duplicate by citing only `B5012D` of
      `{B5012, B5012D}`. It was **not** fooled by the equal totals — the loss was
      row-set completeness, not aggregate reasoning.
- [x] Observed baseline failure modes documented (`BASELINE_RUN_NOTES.md` §3)
- [x] Baseline not modified or re-run after results were seen
- [ ] **BLOCKER — `.gitignore` excludes the real artifacts.** `results/*.json`,
      `outputs/solution/` and `reports/` are all currently ignored, and
      `outputs/baseline/` is untracked. None of the real run evidence is
      committed. See §10 for the recommendation; needs your decision.

## 3. XLSX deliverable

- [x] `make report` generates one workbook per case
- [x] Three sheets per workbook: Summary / Exceptions / Evidence
- [x] All 14 workbooks open cleanly (verified via openpyxl)
- [x] Summary contrasts aggregate tie-out with row-level exception count
- [x] Evidence sheet carries source values for every cited row
- [x] Amounts written as numbers, not text
- [x] Regenerated from the **real** solution outputs; workbooks carry
      `model: minimax/minimax-m2.7`, `provider: openrouter`
- [ ] **Needs the brief:** whether workbooks must be committed, attached to the
      submission, or only reproducible via `make report`

## 4. Reproducibility

- [x] Deterministic generation from master seed 42, generator v1.0
- [x] `make data` reproduces committed datasets byte-for-byte
- [x] Datasets and ground truth committed to the repository
- [x] Dependencies pinned (`jsonschema`, `openpyxl`, `pytest`)
- [x] Full pipeline runs credential-free under `LLM_PROVIDER=mock`
- [x] Agent output is deterministic across runs (asserted by test)
- [x] `.env.example` documents required environment variables
- [x] `make data` verified byte-identical against committed datasets (`diff -r`)
- [x] Evaluator re-run against existing outputs reproduces metrics exactly
      (totals + per_case SHA-256 unchanged) — no API spend needed
- [ ] Confirm on a clean clone + fresh venv (only verified in the development
      environment so far)
- [x] Python version recorded: 3.14.6

## 5. Tests

- [x] `make test` → 113 passed, 0 failed, 0 skipped (re-verified this pass)
- [x] Unit coverage: matching, classification, verifier arithmetic, report
- [x] Integration: end-to-end mock run scored by the frozen evaluator
- [x] Regression: signature case, hostile-model reply changes nothing
- [x] Provider error handling: HTTP 429, error object, missing/malformed choices
- [x] No test requires network or credentials

## 6. Provenance and integrity

- [x] Evaluation contract frozen before implementation (git history)
- [x] Evaluator, schemas, taxonomy, contract SHA-256 unchanged through M5/M6
- [x] Milestone-scoped commits, human review gate at each boundary
- [x] `CHANGELOG.md` written as work happens, not reconstructed
- [x] Baseline is the control and is never tuned after results are seen
- [x] Flagged ambiguity documented rather than silently resolved
      (`docs/EVALUATION.md` §6, undefined "critical break recall")
- [x] `CHANGELOG.md` baseline entry: approach, model, hypothesis, measured
      result, failure modes
- [x] `CHANGELOG.md` solution entry with the measured baseline→solution delta
- [x] Provenance defect documented, including that it remains latent in
      `baseline.py` by choice (frozen control)
- [ ] Rename tracked `CLAUDE.MD` → `CLAUDE.md` if case consistency matters
      (both spellings currently resolve on this case-insensitive filesystem)

## 7. Video / demo

- [x] Script drafted — `docs/DEMO_SCRIPT.md`, built on real measured numbers
- [ ] **Needs the brief:** required length, format and hosting
- [ ] Script the walkthrough: problem → aggregate trap → architecture →
      boundary → signature case → results → XLSX
- [ ] Demonstrate a live run (or a recorded real run) rather than mock-only
- [ ] Show the XLSX Summary sheet flagging the aggregate trap
- [ ] Record, caption if required, and verify the link is publicly accessible
- [ ] Include the link in README and in the submission form

## 8. Competition artifacts — needs confirmation against the brief

The competition brief is **not present in this repository**; the only references
to it are second-hand (`docs/EVALUATION.md` §6 cites brief §9;
`src/recon/baseline.py` cites brief §10). Each item below must be checked
against the actual brief before submission.

- [ ] Submission form / portal fields and deadline
- [ ] Required repository visibility (public vs private + granted access)
- [ ] Licence file, if required
- [ ] Whether "critical break recall" has since been defined by the organizers;
      if so, add a severity field to ground truth and report it (the contract
      is designed to absorb this without other changes)
- [ ] Any required write-up beyond the README (hot take, design doc, one-pager)
- [ ] Page/length limits on written material
- [ ] Whether outputs, reports and results must be committed artifacts

## 10. `.gitignore` — decision required before submission

Current state, verified with `git check-ignore`:

| Path | Present | Tracked | Ignored? |
|---|---|---|---|
| `outputs/baseline/` (real run, 15 files) | yes | **0** | no — merely untracked |
| `outputs/solution/` (real run, 15 files) | yes | **0** | **yes** |
| `results/*.json` (3 real result files) | yes | **0** | **yes** |
| `reports/` (14 real XLSX) | yes | **0** | **yes** |

**None of the real run evidence is currently committable.** The ignore rules
were written when these directories held MockProvider artifacts; they now hold
the real runs.

**Recommended change** — narrow the rules rather than delete them:

```gitignore
# was: results/*.json          -> commit the three real result files
# was: outputs/solution/       -> commit the real solution run
# was: reports/                -> decide per the brief (see §3)
```

Concretely: drop `results/*.json` and `outputs/solution/` from `.gitignore`, add
`outputs/baseline/` and `outputs/solution/` deliberately, and keep ignoring
`reports/` only if the brief does not require the workbooks as artifacts.

**Not changed yet — this affects submission contents and needs your approval.**

## 9. Pre-submission final pass

- [ ] `make test` green on the final commit
- [ ] `make data` → no diff against committed datasets
- [x] Real `make baseline && make solve && make eval` completed and recorded
- [x] All ten README placeholders replaced with measured values
- [x] No placeholder token remains in any submitted document (grep-verified)
- [x] Hot take written from measured results
- [x] No document claims a model other than `minimax/minimax-m2.7` for the real
      runs; no mock result is presented as a real one
- [ ] Video link live and correct
- [ ] Working tree clean; no credentials committed (`.env` is gitignored —
      verified still ignored, and the real key is in it)
- [ ] `.gitignore` decision from §10 applied and real artifacts committed
