# Demo script — "The books balance. The reconciliation is still wrong."

Target 5 minutes (beats below run to ~5:00; trim the optional verifier shot at
2:05–3:05 first if over). Every number below is measured and traceable to
`results/*.json` or `outputs/*/_meta.json` — no figure is estimated. Do **not**
demo a MockProvider run: F1 = 1.0 from mock is not a result.

Required brief confirmation: length, format, hosting (see
`SUBMISSION_CHECKLIST.md` §7).

---

## 0:00–0:35 — The hook

> "Here are two files. An internal ledger, and the partner statement for the
> same period. Gross totals: 6,628.26 and 6,628.26. Net totals: 6,572.04 and
> 6,572.04. They match to the cent. Every reconciliation dashboard I've worked
> with would call this period clean and move on."

**On screen:** `case_13_signature_adversarial` — the two CSVs side by side,
totals highlighted.

> "It isn't clean. There are three separate errors in these files, and they
> happen to cancel."

## 0:35–1:20 — Why the totals lie

**On screen:** the three breaks, with the arithmetic.

| Break | Rows | What's wrong | Net effect on B |
|---|---|---|---|
| COMPOUND (FX + fee) | A1011 / B5011 | same EUR 1234.50 @ 1.0857 → A rounds up to 1340.30, B rounds down to 1340.29; fee 2.50 vs 3.10 | −0.61 |
| DUPLICATE | B5012, B5012D | one 177.51 settlement booked twice | +175.61 |
| MISSING_IN_B | A1013 | a 177.50 payout the partner never reported | −175.00 |

> "Minus 0.61, plus 175.61, minus 175.00. Exactly zero. Equality of sums is one
> number's worth of constraint on a thirteen-row file — any set of errors that
> cancels satisfies it. So the totals cannot tell 'no errors' apart from 'errors
> that happen to cancel.' A payout was mis-booked twice over, a transaction was
> paid twice, and a payout is missing. Three different people need to do three
> different things."

## 1:20–2:05 — Baseline: what one good prompt gets you

> "Control condition: one prompt per case, both files and the output schema,
> a capable model — minimax-m2.7. No tools, no verification."

**On screen:** `results/baseline.json` headline.

> "F1 0.8182. And it's genuinely decent — zero false positives on the clean
> cases, and it never once invented a row ID. On this very case it wasn't fooled
> by the equal totals either: it found two of the three breaks."

**On screen:** the baseline's `case_13` duplicate prediction: `B=[B5012D]`.

> "But look at the duplicate. It reported B5012D. The duplicate is B5012 *and*
> B5012D — a pair. Half a duplicate isn't a duplicate; an analyst can't act on
> that. Elsewhere it called an FX rounding divergence a 'rounding difference,'
> and on one case it emitted `component_causes: null` instead of leaving the
> field out, which invalidated an otherwise-correct answer.
>
> None of those are reasoning failures. They're arithmetic and set membership —
> the parts a `Decimal` comparison and a set equality already do perfectly."

## 2:05–3:05 — Deterministic-first, with the model on a short leash

**On screen:** the five-stage diagram from README §2.

> "So the pipeline decides everything decidable *before* the model is consulted.
> Normalize — Decimal money, canonical references, so `PB/INV-70055`,
> `inv 70055` and `INV-70055` are one identity. Match on reference, then exact
> amount, then near-amount in a date window. Then classify the cause by
> arithmetic: same foreign amount and rate with a one-cent gap is FX rounding,
> not a mystery.
>
> On this evaluation set, that settles thirteen of the fourteen cases. One case
> reaches the model."

**On screen:** `case_11_ambiguous` — B5009 `REF ILLEGIBLE`, B5010 `REF MISSING`.

> "This one. Two identical amounts, same date, and the partner's references read
> 'illegible' and 'missing.' No arithmetic establishes which pairs with which.
> That's a real judgment call, and that's what the model is for.
>
> And when it answers, it gets labels and prose — never numbers. Every amount is
> recomputed from the source rows by a verifier that runs last and drops
> anything citing a row that doesn't exist. In the tests we hand it a
> deliberately hostile reply — fabricated row IDs, wrong arithmetic, and the
> advice 'totals tie out so there's nothing to report' — and the scored output
> doesn't move."

*(Optional, if time: show `verify()` dropping a ghost row ID and rewriting
`888.00` to `-0.60`.)*

## 3:05–3:50 — Results, honestly framed

**On screen:** `results/comparison.json`, then `docs/PER_CASE_COMPARISON.md`.

| | Baseline | Solution |
|---|---|---|
| F1 | 0.8182 | **1.0** |
| Precision / Recall | 0.8571 / 0.7826 | 1.0 / 1.0 |
| Cause accuracy | 0.7778 | 1.0 |
| TP / FP / FN | 18 / 3 / 5 | **23 / 0 / 0** |
| Runtime (14 cases) | 822.1s | **13.9s** |
| LLM calls | 14 | **1** |
| LLM cost | $0.280687 | $0.00088047 |

> "All 23 breaks, no false positives, on the same data with the same scorer.
> 13.9 seconds against 822. One LLM call against fourteen.
>
> Two honest caveats. This is fourteen cases, one seed, one model, one run —
> F1 of 1.0 means no scoring error on *this* set, not general accuracy. And the
> speed and cost gap mostly measures *not calling the model*, so read this as
> deterministic-first architecture versus one-prompt LLM, not as two LLM
> configurations going head to head. The accuracy gap is the like-for-like part."

**On screen:** the XLSX Summary sheet for `case_13`.

> "And the deliverable an analyst actually opens says the quiet part out loud:
> aggregate totals tie out — YES. Row-level exceptions — three. Aggregate check
> misleading — yes."

## 3:50–4:25 — Held out, and the experiment we threw away

**This is the strongest 30 seconds. Do not cut it.**

**On screen:** the seed-42 vs seed-777 table.

| | Baseline 42 | Baseline 777 | agent-v1 42 | agent-v1 777 |
|---|---|---|---|---|
| F1 | 0.8182 | **0.72** | 1.0 | **1.0** |
| Precision | 0.8571 | **0.6667** | 1.0 | 1.0 |
| FP | 3 | **9** | 0 | 0 |

> "Same generator, new seed, values the system had never seen. The agent held at
> 1.0. The one-prompt baseline fell to 0.72 — precision only, false positives
> tripled from 3 to 9. Same prompt, same model, different numbers.
>
> To be precise about what that proves: seed-42 and seed-777 share the same case
> structure, so this shows robustness to *value* variation. It does not show
> robustness to new break structures or to real partner data. I'm not claiming
> general accuracy from fourteen cases."

**On screen:** the ablation table.

> "And the experiment I threw away. I forced cause classification through the
> LLM instead of the arithmetic — everything else identical, same verifier
> downstream. Result: **fifteen times the cost for worse accuracy.** F1 dropped
> to 0.9778, cause accuracy to 0.8636.
>
> **And verifier corrections went from zero to four.** That's the number I care
> about. Three times it silently fixed amounts the wrong label would have
> corrupted. Once it dropped a break entirely — because the model returned its
> component causes as a *sentence* instead of an enum value. Its prose was
> right about the rounding mode. The field was unusable. Rejected, and it's in
> the repo as a rejected experiment, not quietly deleted."

## 4:25–4:45 — How it got here

**On screen:** `CHANGELOG.md`, scrolled.

> "Every one of those decisions is in the changelog with what was measured and
> whether it was kept — the contract frozen before any implementation, the
> baseline never retuned after I saw its score, the provenance bug where the
> model confabulated eight different model names for itself, and this rejected
> ablation. Written as the work happened, not reconstructed afterwards."

## 4:45–5:00 — Close

> "The finding I didn't expect: we took the language model out of 93% of the
> work and accuracy went *up*. The model earns its place at the ambiguous
> edges — the illegible reference, the judgment call — under a verifier that
> recomputes everything it touches.
>
> The books balanced. The reconciliation was still wrong. Now we can prove it,
> row by row."

---

## Shot list

| Shot | Source |
|---|---|
| Two CSVs, totals highlighted | `data/cases/case_13_signature_adversarial/source_*.csv` |
| Three breaks + arithmetic | `data/cases/.../ground_truth.json`, README §4 |
| Baseline headline | `results/baseline.json` |
| Baseline's partial duplicate | `outputs/baseline/case_13_signature_adversarial.json` |
| Five-stage diagram | README §2 |
| Ambiguous case references | `data/cases/case_11_ambiguous/source_b.csv` |
| Verifier drop/correct (optional) | `tests/test_agent_integration.py` regression tests |
| Comparison table | `results/comparison.json`, `docs/PER_CASE_COMPARISON.md` |
| XLSX Summary sheet | `docs/sample_exception_report_case13.xlsx` |
| Holdout table | `docs/HOLDOUT_SEED777.md`, README §6.5 |
| Ablation table + 4 corrections | `docs/ABLATION_LLM_CAUSE.md`, `outputs/ablation-llm-cause/_meta.json` |
| Changelog scroll | `CHANGELOG.md` |

## Things to not say

- Don't say "1 of 14 LLM calls" as a property of the architecture — say
  "measured on this 14-case set."
- Don't present runtime/cost as an LLM-vs-LLM comparison.
- Don't show a MockProvider run or quote a mock F1.
- Don't claim generalization from 14 cases.
- Don't call the holdout an independent dataset — it shares the generator.
- Don't skip the ablation because it's a negative result; it's the best evidence
  the design decision was load-bearing.
- Don't call the baseline bad: it scored 0.8182 with zero clean-case false
  positives and no hallucinated row IDs. The story is *where* it failed.
