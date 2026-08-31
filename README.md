# Reconciliation Exception Triage Agent

An agentic workflow that reconciles an internal ledger (Source A) against a
partner statement (Source B), detects row-level breaks, classifies their cause,
and produces an evidence-backed exception report (XLSX) for human review.
Built for the micro1 Frontier Engineering Challenge 2026.

**Demo video:** _<!-- VIDEO_LINK: replace this line with the unlisted URL once recorded -->_
**Sample deliverable:** [docs/sample_exception_report_case13.xlsx](docs/sample_exception_report_case13.xlsx)
— the exception report for the signature adversarial case, generated from the real agent-v1 run.

**User:** finance/operations analysts reconciling transactions between two sources.
**Core hypothesis:** aggregate correctness != row-level correctness.
**Evaluation contract:** [docs/EVALUATION.md](docs/EVALUATION.md) — frozen before
any agent implementation, and unchanged since (verifiable in git history).

> **Results status.** Real runs complete. Baseline and solution both ran on
> OpenRouter with `minimax/minimax-m2.7` over the same 14 committed cases and
> the same deterministic scorer. Headline: **F1 0.8182 → 1.0**. Every figure in
> this README is read from `results/*.json` and `outputs/*/_meta.json`; none is
> estimated. Read [§6 Results](#6-results) together with its scope disclosure —
> the two systems differ in LLM usage by design (14 calls vs 1), so this is a
> comparison of *architectures*, not of two LLM configurations.

---

## 1. Problem, user, and the actual bottleneck

Reconciliation tools routinely confirm that two sources agree **in aggregate**
and report the period as clean. Aggregate agreement is a much weaker statement
than row-level agreement: offsetting errors cancel in the totals while every
underlying transaction remains wrong. This project treats row-level break
detection as the task, and deliberately includes cases where the totals tie out
and the rows do not.

**Intended user:** a finance/operations analyst reconciling transactions between
an internal ledger and a partner statement, who must sign off on a period and
act on each exception.

**The actual bottleneck — measured, not assumed.** The bottleneck is not
"the model cannot reason about reconciliation." On the seed-42 evaluation set the
one-prompt LLM baseline scored **F1 0.8182 with zero clean-case false positives
and zero hallucinated row IDs** — it is broadly competent at spotting that
something is wrong. It failed on three mechanical things:

1. **Row-set completeness on multi-row breaks** — citing one row of a two-row
   duplicate (`case_10` ×2, `case_13`).
2. **Cause attribution requiring arithmetic** — calling an FX rounding-mode
   divergence a rounding difference (`case_08` ×2), and giving a COMPOUND the
   wrong component set (`case_12`, `case_13`).
3. **Schema formatting** — emitting `component_causes: null` instead of omitting
   the field, invalidating an otherwise-correct answer (`case_04`, −2 TP).

Every one of those is decidable by a `Decimal` comparison, a set equality, or a
schema. That is the bottleneck this design attacks: not reasoning capacity, but
the fact that arithmetic and set membership were being delegated to a component
that does them probabilistically. The
[rejected ablation](docs/ABLATION_LLM_CAUSE.md) tests that claim directly.

## 2. Architecture

Five stages. The ordering is the point: everything that can be decided by
arithmetic is decided before the model is consulted, and everything the model
says is re-checked against the source rows afterwards.

```
  source_a.csv ─┐
                ├─▶ 1. normalize ──▶ 2. candidate matching ──▶ 3. deterministic
  source_b.csv ─┘    (Decimal money,     (reference / amount /     classification
                      canonical refs)     date heuristics)          (arithmetic)
                                                                        │
                                            ┌───────────────────────────┘
                                            ▼
                                    4. LLM reasoning          ◀── unsettled
                                    (labels + prose ONLY)         candidates only
                                            │
                                            ▼
                                    5. deterministic verifier ◀── THE OUTPUT GATE
                                            │
                                            ▼
                       outputs/solution/<case_id>.json + _meta.json
                                            │
                                            ▼
                          reports/<case_id>.xlsx (3 sheets)
```

| Stage | Module | Responsibility | LLM? |
|---|---|---|---|
| 1. normalize | `src/recon/agent.py` | CSV → typed rows; `Decimal` money; canonical references (`PB/INV-70055`, `inv 70055`, `INV-70055` → `INV70055`); parsed dates | no |
| 2. candidate matching | `src/recon/agent.py` | unique-reference pass → exact `(currency, gross, fee)` pass → near-amount within a date window; duplicate grouping by economic signature; ambiguous-cluster detection | no |
| 3. classification | `src/recon/agent.py` | cause from arithmetic: FX attribution, rounding tolerance, gross/fee deltas, `COMPOUND` when ≥2 causes apply | no |
| 4. LLM reasoning | `src/recon/agent.py` | interpretation of candidates code could not settle: break-type label, confidence, one-line rationale, suggested action | **yes** |
| 5. verification | `src/recon/agent.py` | recompute every amount from source rows; drop breaks citing nonexistent row IDs, types outside the taxonomy, or unsupported `COMPOUND` component sets; log every intervention | no |
| report | `src/recon/report.py` | XLSX: Summary / Exceptions / Evidence | no |

Supporting modules: `generate.py` (deterministic datasets), `evaluate.py`
(deterministic scorer), `baseline.py` (one-prompt control), `providers.py`
(OpenRouter / Ollama / Mock), `taxonomy.py` (authoritative break enum).

### 2.1 The deterministic / LLM boundary

This is the central design claim, so it is worth stating precisely.

**The LLM contributes labels and prose. It never contributes a number, and it
never decides which rows are involved in a break.**

The boundary is *structural*, not merely instructed. The prompt does tell the
model never to do arithmetic — but the enforcement does not rely on it obeying:

| Concern | Who decides | Why the LLM cannot affect it |
|---|---|---|
| Which rows form a break | stage 2 partition | row sets are taken from the matcher's output; the model's reply has no channel to add, remove, or substitute a row ID |
| `amount_a`, `amount_b`, `difference` | stage 5 `_recompute()` | recomputed from source rows and **overwritten**; a disagreeing claim is replaced and logged as `CORRECTED_ARITHMETIC` |
| Whether a `COMPOUND` component set is legitimate | stage 5 `_supported_causes()` | re-derived from the rows; an unsupported set is dropped |
| Whether a break type is valid | stage 5 | checked against the frozen taxonomy; anything else is dropped |
| Cited row IDs exist | stage 5 | a break citing an unknown row ID is dropped before output |

What the model *is* allowed to change: the break-type label where stage 3 left
the cause genuinely open (i.e. `AMBIGUOUS` clusters), the confidence level, the
free-text rationale appended to evidence, and the suggested action.

Two further properties, both covered by tests:

- **Sparing invocation.** The model is called only for candidates the
  deterministic stages could not settle. **Measured on this 14-case evaluation
  set, that was 1 of 14 cases** (`case_11_ambiguous`); the other 13 were
  resolved deterministically with no LLM call. This ratio is a property of
  *this dataset's* ambiguity density, not a universal property of the
  architecture — a corpus with more unusable references, truncated
  identifiers, or many-to-one settlement batches would route more cases to the
  model. See [§6.3](#63-scope-and-limitations).
- **Failure degrades, never corrupts.** A malformed reply, a missing field, or
  a transport failure falls back to the deterministic result and records the
  error in `_meta.json`. The run continues.

Every LLM decision (accepted or rejected) and every verifier intervention is
written to `outputs/solution/_meta.json`, so a run is auditable after the fact.

### 2.2 Provider error handling

`providers.py` raises `ProviderError` carrying the HTTP status and the
provider's own error code/message. Guarded paths: `HTTPError` / `URLError`,
non-JSON bodies, an `error` object inside an HTTP 200, a response missing
`choices`, and empty/malformed `choices` / `message` / `content`. There are no
retries and no model or provider fallbacks — a failed run must be visibly
failed rather than quietly recorded as an empty result.

## 3. Evaluation

Scoring semantics are frozen in [docs/EVALUATION.md](docs/EVALUATION.md).
Summary of what the scorer does:

- **Detection TP** requires **exact row-set identity** on both sides:
  `set(p.a_row_ids) == set(g.a_row_ids)` and likewise for B. Matching is
  one-to-one; `DUPLICATE` and `COMPOUND` get no partial credit.
- **Primary metric:** break-detection **F1**, micro-averaged (TP/FP/FN pooled
  across cases).
- **Secondary:** precision, recall, cause-classification accuracy (over TPs),
  evidence-validity rate (over all predictions), false positives on clean
  cases, runtime per case, cost per run.
- A prediction citing any nonexistent row ID is an FP **and** evidence-invalid,
  regardless of its other fields.
- The scorer is deterministic and LLM-free: no judge model, no credentials.

`docs/EVALUATION.md` §6 records a flagged ambiguity: the competition brief
proposes "critical break recall" as the primary metric but does not define
"critical". Rather than invent severity semantics, the contract uses row-level
F1 as primary and reports recall explicitly as a named secondary metric. A
severity field can be added to ground truth later without disturbing the rest
of the contract.

### 3.1 Dataset

14 cases, generated deterministically from master seed 42 (generator v1.0) and
committed to the repository. **23 planted breaks total.** Baseline and solution
run on exactly the same committed files.

| Case | A rows | B rows | Breaks | Net totals equal? | Break types |
|---|---|---|---|---|---|
| case_01_clean_small | 12 | 12 | 0 | yes | — (false-positive pressure) |
| case_02_clean_messy | 16 | 16 | 0 | yes | — (ref noise + date shifts) |
| case_03_missing_in_b | 12 | 10 | 2 | no | MISSING_IN_B ×2 |
| case_04_missing_in_a | 10 | 12 | 2 | no | MISSING_IN_A ×2 |
| case_05_amount_mismatch | 12 | 12 | 2 | no | AMOUNT_MISMATCH ×2 |
| case_06_fee_mismatch | 12 | 12 | 2 | no | FEE_MISMATCH ×2 |
| case_07_amount_and_fee | 12 | 12 | 2 | no | AMOUNT_MISMATCH, FEE_MISMATCH |
| case_08_fx_difference | 10 | 10 | 2 | no | FX_DIFFERENCE ×2 |
| case_09_rounding | 12 | 12 | 2 | no | ROUNDING_DIFFERENCE ×2 |
| case_10_duplicates | 12 | 14 | 2 | no | DUPLICATE ×2 |
| case_11_ambiguous | 10 | 10 | 1 | **yes** | AMBIGUOUS |
| case_12_compound | 11 | 11 | 1 | no | COMPOUND (FEE + FX) |
| **case_13_signature_adversarial** | 13 | 13 | 3 | **yes** | COMPOUND, DUPLICATE, MISSING_IN_B |
| case_14_aggregate_trap | 12 | 12 | 2 | **yes** | AMOUNT_MISMATCH ×2 |

Four cases have equal A/B net totals while containing breaks. A system that
reasons from totals scores zero recall on all four.

## 4. The signature adversarial case

`case_13_signature_adversarial` exists to make the core hypothesis falsifiable
rather than rhetorical. All figures below are read directly from the committed
`source_a.csv`, `source_b.csv` and `ground_truth.json`.

**Aggregate view.** A gross `6628.26` = B gross `6628.26`; A net `6572.04` =
B net `6572.04`. Both totals tie out exactly. An aggregate check reports clean.

**Row-level view.** Ground truth contains three breaks:

| GT | Type | Rows | Source-row arithmetic | Net effect on B |
|---|---|---|---|---|
| GT-001 | COMPOUND (FEE_MISMATCH + FX_DIFFERENCE) | A1011 / B5011 | same EUR 1234.50 @ 1.0857 → A gross 1340.30 (HALF_UP) vs B 1340.29 (DOWN) = 0.01; fee A 2.50 vs B 3.10 = −0.60; net A 1337.80 vs B 1337.19 | −0.61 |
| GT-002 | DUPLICATE | B5012, B5012D | identical reference/currency/gross 177.51/fee 1.90 booked twice in B; net 175.61 each | +175.61 |
| GT-003 | MISSING_IN_B | A1013 | A row gross 177.50, fee 2.50, net 175.00 with no B counterpart | −175.00 |

**Why the totals tie out:** `−0.61 + 175.61 − 175.00 = 0.00` exactly. Three
independent, genuine errors cancel in the aggregate.

**Why aggregate equality does not establish row-level correctness.** Equality
of sums is a single scalar constraint on a many-dimensional object. Any set of
per-row errors whose signed sum is zero satisfies it, so the totals cannot
distinguish "no errors" from "errors that happen to cancel". Concretely, in
this case a totals-only check reports clean while: one payout is mis-booked in
both FX rounding and fee, one transaction is paid twice, and one payout is
missing from the partner statement — three separate items an analyst must act
on, with different remediations. `case_14_aggregate_trap` makes the same point
minimally: two amount errors of `+0.75` and `−0.75`.

The XLSX Summary sheet therefore reports aggregate tie-out and row-level
exception count side by side, and prints
`Aggregate check misleading: YES — totals tie out but row-level breaks exist`
when both hold, so the trap cannot be read as a clean result.

## 5. Reproduction

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # then fill in OPENROUTER_API_KEY / OPENROUTER_MODEL
                         # and set LLM_PROVIDER=openrouter
```

```bash
make data       # M2: regenerate the 14 committed cases (seed 42) — byte-identical
make baseline   # M4: one-prompt baseline over all cases -> outputs/baseline/
make solve      # M5: agent pipeline over all cases      -> outputs/solution/
make eval       # M3: score both -> results/baseline.json, solution.json, comparison.json
```

Additional targets:

```bash
make report     # M6: XLSX exception reports -> reports/<case_id>.xlsx
make test       # full test suite
```

**Sample artifact.** [`docs/sample_exception_report_case13.xlsx`](docs/sample_exception_report_case13.xlsx)
is committed as the sample deliverable — the `case_13_signature_adversarial`
workbook, generated from the real agent-v1 seed-42 run, three sheets
(Summary / Exceptions / Evidence). `reports/` itself is gitignored; `make report`
regenerates all 14 workbooks there from the committed `outputs/solution/`. Step-by-step setup: **[REPRODUCTION.md](REPRODUCTION.md)**.

`make data` is idempotent: regeneration from seed 42 reproduces the committed
datasets byte-for-byte, so the committed ground truth can be independently
re-derived.

**Running without credentials.** With `LLM_PROVIDER=mock` (the default in
`.env.example`), the entire pipeline — generation, agent, evaluation, XLSX,
tests — runs offline against `MockProvider`. This exercises plumbing only; it
is not a measurement of model quality.

**Note on `make eval`.** The target scores baseline and solution and then
compares them, so it requires both `outputs/baseline/` and `outputs/solution/`
to be populated; run `make baseline` and `make solve` first.

## 6. Results

Both systems ran on the same 14 committed cases (23 planted breaks), scored by
the same frozen deterministic evaluator. Provenance for both is taken from the
runner-written `_meta.json`, not from model self-report (see
[§6.4](#64-provenance)).

**Provider `openrouter`, model `minimax/minimax-m2.7`** for both arms.

| Metric | Baseline (one prompt) | Solution (agent) | Delta |
|---|---|---|---|
| **Break-detection F1 (primary)** | **0.8182** | **1.0** | **+0.1818** |
| Precision | 0.8571 | 1.0 | +0.1429 |
| Recall | 0.7826 | 1.0 | +0.2174 |
| Cause-classification accuracy (over TPs) | 0.7778 | 1.0 | +0.2222 |
| Evidence-validity rate | 1.0 | 1.0 | 0.0 |
| False positives on clean cases | 0 | 0 | 0 |
| TP / FP / FN | 18 / 3 / 5 | 23 / 0 / 0 | — |
| Wall clock (14 cases) | 822.1s | 13.9s | −808.2s |
| LLM calls | 14 | 1 | −13 |
| LLM cost (USD) | $0.280687 | $0.00088047 | −99.7% |
| Verifier corrections | n/a | 0 | — |

Per-case TP/FP/FN with runtimes: **[docs/PER_CASE_COMPARISON.md](docs/PER_CASE_COMPARISON.md)**.
Run observations, including a large runtime outlier: **[docs/BASELINE_RUN_NOTES.md](docs/BASELINE_RUN_NOTES.md)**.

### 6.1 Where the baseline lost points

The baseline detected most breaks. It lost points in three specific, diagnosable
ways — all on the parts of the task that are arithmetic or set-membership rather
than judgment:

| Case | Failure | Cost |
|---|---|---|
| `case_10_duplicates` | Attached a spurious A-side row to each duplicate — predicted `A=[A1011], B=[B5011,B5011D]` where truth is `A=[], B=[B5011,B5011D]`. Exact row-set identity fails. | −2 TP, +2 FP, +2 FN |
| `case_13_signature_adversarial` | Reported the duplicate as `B=[B5012D]` alone, omitting its pair `B5012`. Half a duplicate is not a duplicate. | −1 TP, +1 FP, +1 FN |
| `case_04_missing_in_a` | Both row sets correct, but the reply emitted `component_causes: null` instead of omitting the field. The output failed schema validation and was scored honestly as unscoreable. | −2 TP, +2 FN |
| `case_08_fx_difference` (×2) | Detected both, labelled them `ROUNDING_DIFFERENCE` rather than `FX_DIFFERENCE`. | cause only |
| `case_12_compound`, `case_13` | Correct `COMPOUND` detection, wrong components: `[AMOUNT_MISMATCH, FEE_MISMATCH]` instead of `[FEE_MISMATCH, FX_DIFFERENCE]`. | cause only |

Notably the baseline scored **0 false positives on the clean cases** and **1.0
evidence validity** — it never invented a row ID. Its weaknesses were row-set
precision on multi-row breaks, cause attribution requiring arithmetic, and one
schema-formatting slip. Each is something the deterministic stages settle by
construction rather than by asking a model to be careful.

### 6.2 The signature adversarial case

On `case_13_signature_adversarial` — where gross and net totals tie out exactly
while three row-level breaks exist — the baseline found **2 of 3** breaks and
the solution found **3 of 3**.

The baseline did *not* fall for the aggregate trap: it looked at rows and found
real breaks. It failed on **row-set completeness**, citing only `B5012D` for a
duplicate that is `{B5012, B5012D}`. Under exact row-set identity that is an FP
plus an FN, not partial credit — an analyst told "row B5012D is duplicated"
without its pair cannot act on it.

### 6.3 Scope and limitations

Stated plainly, because the headline numbers invite over-reading:

- **14 cases, 23 breaks, one seed (42), one model, one run each.** F1 = 1.0 means
  the solution made no scoring error on *this* set. It is not a claim of general
  reconciliation accuracy, and a single-run result carries no variance estimate.
- **Resource comparison, NOT a like-for-like LLM comparison.** On these
  datasets the **baseline used 14 LLM calls and agent-v1 used 1.** The runtime
  and cost gaps (−98.3%, −99.7%) therefore mostly measure *not calling the
  model*. They compare two architectures — deterministic-first pipeline vs
  one-prompt LLM — and must not be quoted as one model configuration
  outperforming another. The accuracy delta is the like-for-like part: same
  model, same data, same scorer.
- **The solution's accuracy is overwhelmingly deterministic.** 13 of 14 cases
  never reached the model. The LLM contributed labels and prose on one
  ambiguous case and could not have affected any amount or row set (§2.1).
- **F1 = 1.0 does not prove general accuracy.** It means agent-v1 made no
  scoring error on these specific cases. Stated explicitly:
  **seed-42 and seed-777 share the same generator and the same case structure.**
  The holdout establishes **robustness to value variation under that structure**
  — new amounts, dates, references, currencies. It does **not** establish
  robustness to unseen break structures, unseen reference-noise patterns,
  many-to-one reconciliation (batch settlements), or real partner data. Because
  both seeds come from one generator, and the matcher's reference normalization
  was written against that generator's noise styles, **the holdout cannot detect
  overfitting to the generator itself.** A stronger test needs a different
  generator or real files; neither was run.
- **Baseline runtime is outlier-dominated.** One case (`case_09_rounding`) took
  420.34s — 51% of total wall clock and 87% of run cost. The median baseline
  case took ~28s. Quote the median alongside the total.
- **Verifier corrections were 0 on this run**, because the deterministic stages
  supplied every amount and the single LLM reply stayed inside its remit. The
  verifier's value is demonstrated by regression tests that feed it a hostile
  reply (§7), not by this run's counter.

### 6.4 Provenance

Both `results/*.json` files take `model` and `provider` from the runner-written
`_meta.json`. This matters: in the real baseline run the model **misreported its
own identity in all 14 case files**, claiming 8 distinct model ids across 3
providers (`gpt-4o`, `gpt-4`, `o4-mini`, `claude-3-5-sonnet-20241022`,
`reconciliation-engine`, `reconciliation-core`, `reconciliation-v1`,
`recon-v1`) while actually running `minimax/minimax-m2.7`. The discarded
self-report is preserved under `system_self_reported` for audit. A model asked
to describe itself will confabulate; only runner-side metadata is evidence.

### 6.5 Held-out generalization check (seed 777) — secondary result

A separate dataset was generated with the same unmodified generator at **seed
777** (`data-holdout/`) and both systems re-run on it with the same model and
provider. **This is a secondary result; the primary experiment above is
seed-42.** Full detail: **[docs/HOLDOUT_SEED777.md](docs/HOLDOUT_SEED777.md)**.

| Metric | Baseline 42 | Baseline 777 | agent-v1 42 | agent-v1 777 |
|---|---|---|---|---|
| F1 | 0.8182 | **0.72** | 1.0 | **1.0** |
| Precision | 0.8571 | **0.6667** | 1.0 | 1.0 |
| Recall | 0.7826 | 0.7826 | 1.0 | 1.0 |
| TP / FP / FN | 18/3/5 | 18/**9**/5 | 23/0/0 | 23/0/0 |

**agent-v1 held at F1 1.0 on unseen values.** The baseline dropped to 0.72 —
purely through precision, with false positives tripling from 3 to 9 (on holdout
`case_13` it emitted 9 predictions for 3 breaks). TP, FN, recall and cause
accuracy were unchanged, so the one-prompt approach is less stable across value
variation than its seed-42 score suggested.

Two caveats that matter more than the numbers:

- **The holdout is a structural replica, not an independent dataset.** Same case
  list, row counts, break-type composition and 23-break total; only values vary
  (97% of 334 rows differ in reference and amount). Both seeds share one
  generator, so this **cannot** detect overfitting to the generator itself.
  It tests robustness to value variation under fixed structure — nothing more.
- **`clean_case_false_positives` reads 0 but understates the baseline.** On the
  holdout's clean `case_02` the baseline emitted 6 breaks using an invented type
  `DATE_MISMATCH`; that output failed schema validation, and the metric counts
  only scored FPs. The scorer behaves as specified; the metric is blind to this
  failure shape.

### 6.6 Rejected ablation — LLM cause classification

To test whether the deterministic cause-classification layer earns its place, a
controlled ablation replaced it with the LLM: every candidate break's
`break_type` (and COMPOUND components) decided by the model instead of by
arithmetic, with everything else identical — same matching, same computed
amounts, same prompt, **same verifier still downstream**, same model, same
seed-42 data. Full detail: **[docs/ABLATION_LLM_CAUSE.md](docs/ABLATION_LLM_CAUSE.md)**.

| Metric | agent-v1 | Ablation | Delta |
|---|---|---|---|
| F1 | 1.0 | 0.9778 | −0.0222 |
| Recall | 1.0 | 0.9565 | −0.0435 |
| Cause accuracy | 1.0 | 0.8636 | **−0.1364** |
| TP / FP / FN | 23/0/0 | 22/0/1 | −1 TP, +1 FN |
| Verifier corrections | 0 | 4 | +4 |
| LLM calls | 1 | 12 | ×12 |
| Runtime | 13.9s | 129.05s | ×9.3 |
| Cost | $0.00088047 | $0.01309202 | **×14.9** |

**REJECTED.** Forcing LLM cause classification increased calls and cost while
reducing accuracy. The model agreed with the arithmetic on 19 of 23 candidates;
all four divergences were the same error — failing to attribute a one-cent gross
gap to FX rounding-mode divergence on an identical foreign amount and rate,
reproducing the baseline's failure even when handed the computed difference and
the FX fields. On `case_12` it returned `component_causes` as prose rather than
enum values, so the taxonomy filter emptied the set and the verifier dropped the
break entirely — the lost TP came from a formatting habit, not faulty reasoning.
The ablation remains available as an explicitly opt-in experiment
(`LLM_CAUSE_ABLATION=1`) and is **not** part of the default design.

### 6.7 Method note

The baseline is the experimental control: one prompt, one call per case, no
tools, no verification pass, no retries. **It was not modified after its results
were seen** (`docs/EVALUATION.md` §7). Both systems ran on identical committed
datasets with the same scorer, and the resource difference between them is
disclosed above.

## 7. Tests

**113 tests, all passing** (`make test`), no credentials or network required.

| Area | Tests |
|---|---|
| Contract / schema sync | 7 |
| Generator determinism | 7 |
| Evaluator scoring semantics | 14 |
| Baseline plumbing | 5 |
| Agent: normalize, match, classify, verify, LLM boundary | 31 |
| Agent: integration + signature-case regression | 12 |
| Provider error handling (mocked urllib) | 26 |
| XLSX report | 11 |

Regression tests of note: a deliberately hostile model reply — fabricated row
IDs, wrong amounts, an unsupported `COMPOUND` component set, and the advice
"totals tie out so there is nothing to report" — is asserted to change nothing
in the scored output on the signature case.

Tests run against `MockProvider` and never touch the network. They verify
plumbing and the LLM boundary; they are **not** the source of any reported
metric. All numbers in §6 come from the real OpenRouter runs.

## 8. Repository layout

```
docs/EVALUATION.md     frozen evaluation contract (M1)
docs/PER_CASE_COMPARISON.md  per-case baseline vs solution, from results/*.json
docs/BASELINE_RUN_NOTES.md   real-run observations: provenance, case_09 outlier
docs/SUBMISSION_CHECKLIST.md submission readiness
schemas/               JSON Schemas: transaction, ground_truth, agent_output
src/recon/
  taxonomy.py          authoritative break-type enum + rounding tolerance
  generate.py          deterministic dataset generator (M2)
  evaluate.py          deterministic scorer (M3)
  baseline.py          one-prompt baseline control (M4)
  agent.py             five-stage agent pipeline (M5)
  report.py            XLSX exception report (M6)
  providers.py         OpenRouter / Ollama / Mock providers
data/cases/<case_id>/  source_a.csv, source_b.csv, ground_truth.json
data/manifest.json     case manifest: seeds, row counts, break counts, totals
tests/                 113 tests
CHANGELOG.md           improvement log, recorded as work happens
```

## 9. Model, tool, and data disclosure

- **LLM runs (all of them):** `minimax/minimax-m2.7` via **OpenRouter**,
  temperature 0. Used for the seed-42 baseline and agent-v1, the seed-777
  holdout of both, and the rejected ablation. No other model was used for any
  reported result.
- **Development:** **Claude Code** (Opus) plus Claude.ai sessions for
  scaffolding and implementation, under the milestone gates recorded in
  `CLAUDE.md`; every milestone boundary is a human review gate.
- **Disclosed defect — baseline provenance / model self-report.** The baseline
  runner builds each case's `system` block as
  `{name, model, provider, **model_supplied_system}`, spreading the model's own
  JSON last. The model therefore overwrote the runner's true values and
  **misreported its own identity in all 14 seed-42 case files**, claiming 8
  distinct model ids across 3 providers (`gpt-4o`, `gpt-4`, `o4-mini`,
  `claude-3-5-sonnet-20241022`, `reconciliation-engine`, `reconciliation-core`,
  `reconciliation-v1`, `recon-v1`) while actually running
  `minimax/minimax-m2.7`. `results/baseline.json` initially reported
  `gpt-4o` / `openai`. Fixed in the evaluator (`load_run_provenance()` reads the
  runner-written `_meta.json`); the discarded self-report is preserved under
  `system_self_reported` for audit. The committed baseline case files are left
  **unaltered** — they are the frozen control's raw output and the evidence for
  this finding. The spread order in `baseline.py` remains latent by choice; see
  [docs/BASELINE_RUN_NOTES.md](docs/BASELINE_RUN_NOTES.md) §1. A model asked to
  describe itself will confabulate; only runner-side metadata is evidence.

### 9.1 Data and privacy

- **All datasets are fully synthetic**, produced by `src/recon/generate.py`.
- **No real customer, partner, or production data** is present anywhere in this
  repository, and none was used at any point.
- **Seeded and reproducible:** seed 42 (primary) and seed 777 (holdout),
  generator v1.0. `make data` regenerates the committed seed-42 files
  byte-for-byte (verified with `diff -r`).
- Amounts, references, dates, currencies and merchant descriptions are drawn
  from fixed lists and a seeded PRNG. No PII, no account numbers, no real
  identifiers.
- The only credential involved is an OpenRouter API key, read from `.env`, which
  is gitignored and has never been committed (verified against full history).

### 9.2 Human review position

This system produces an **exception report for human sign-off**. It does not
post adjustments, clear breaks, or approve a reconciliation period, and it is
not designed to run unattended:

- Every break carries the row IDs, the source values, the computed difference
  and a suggested action, so an analyst can verify each finding against the
  ledger rather than trusting the output.
- The `AMBIGUOUS` classification exists precisely to escalate rather than guess:
  where correspondence cannot be established from the data, the report says so
  instead of inventing a pairing.
- Confidence is reported per break, and the XLSX Summary sheet states plainly
  when aggregate totals tie out while row-level breaks exist — the case most
  likely to be waved through.
- The intended workflow is **agent proposes, analyst disposes**. On a 14-case
  synthetic benchmark with one model and one run, that is the only defensible
  posture; see [§6.3](#63-scope-and-limitations).
- **Runtime model (both arms):** `minimax/minimax-m2.7` via **OpenRouter**.
  Recorded in each run's `_meta.json` and carried into `results/*.json`.
  Ollama is supported; `MockProvider` backs the credential-free test suite.
- **Measured spend:** $0.280687 (baseline, 14 calls) + $0.00088047 (solution,
  1 call) = **$0.28157** for the full evaluation.
- **Dependencies:** `jsonschema`, `openpyxl`, `pytest`, all version-pinned in
  `requirements.txt`. Data generation, evaluation and reporting are stdlib +
  these pins only.

## 10. Improvement changelog

See [CHANGELOG.md](CHANGELOG.md). Entries are written as work happens, never
reconstructed retroactively, and record what was tried, what was measured, and
whether it was kept.

## 11. Hot take

**Most of reconciliation should never reach a language model.**

The measured baseline was not incompetent — 0.8182 F1, zero clean-case false
positives, never a hallucinated row ID. It failed where the task is arithmetic
and set membership: it called an FX rounding divergence a rounding difference,
cited one row of a two-row duplicate, and emitted `component_causes: null` in a
way that invalidated an otherwise-correct answer. Those are not reasoning
failures you fix with a better prompt. They are the parts of the job a
`Decimal` comparison and a set equality already do perfectly, for free, every
time.

So the interesting result is not that the agent scored 1.0. It is *where the
model turned out to be necessary*: **1 of 14 cases** on this set — the one where
two identical amounts on the same date carried references reading
`REF ILLEGIBLE` and `REF MISSING`, and no arithmetic could establish
correspondence. That is a genuine judgment call, and exactly the kind of thing
a model should be asked. The other 13 cases were arithmetic wearing a trench
coat.

The corollary is uncomfortable for agent demos: the honest headline here is
"we removed the LLM from 93% of the work and the accuracy went up." The model
earns its place at the ambiguous edges, under a verifier that recomputes
everything it touches — and the engineering that mattered most was deciding
which decisions it was never allowed to make.

