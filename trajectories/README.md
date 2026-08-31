# Trajectories

Two kinds of trajectory are committed here.

## 1. Development trajectories — Claude Code session transcripts

Raw `.jsonl` exports from `~/.claude/projects/`. Every prompt, tool call, file
edit, command and result across the build. Nothing has been edited or trimmed.

| File | Covers |
|---|---|
| `session-main-m1-to-submission.jsonl` (2.3 MB) | The whole build: M1 contract → M2 generator → M3 evaluator → M4 baseline → M5 agent → M6 XLSX → real runs → holdout → ablation → submission docs |
| `session-early-scaffold.jsonl` (16 KB) | Initial scaffolding session before the main build |

**Milestone map for the main session**, in order of appearance:

| Milestone | What happens in the transcript |
|---|---|
| M1 | Evaluation contract frozen before implementation; the undefined "critical break recall" ambiguity is flagged rather than invented (`docs/EVALUATION.md` §6) |
| M2 | Deterministic generator, 14 committed cases, the signature adversarial case constructed so aggregates cancel |
| M3 | Deterministic evaluator, exact row-set identity, six-way diagnostics |
| M4 | Baseline runner + providers; **real run blocked twice on missing credentials** — the run was refused rather than faked with MockProvider |
| M5 | Agent pipeline in four commits; two matcher bugs found and fixed mid-build (ambiguity ordering, cluster guard) |
| M6 | XLSX report, three sheets |
| Real runs | Baseline F1 0.8182; agent-v1 F1 1.0; the provenance defect found and fixed |
| Holdout | seed-777 generation, both arms re-run, structural-replica limitation identified |
| Ablation | LLM cause classification, rejected; a crash in the ablation harness fixed and the run restarted from scratch |
| Submission | README, REPRODUCTION.md, clean-env test, sample artifact |

Worth reading for: several points where a result was refused rather than
fabricated (the credential-blocked baseline), a metric that flattered the
baseline being disclosed instead of quoted (`clean_case_false_positives`), and
self-corrections mid-task (the double-read of an `HTTPError` stream, a
miscounted "5 fabricated identities" that was actually 8).

**No credentials.** Both files scanned: the only `sk-or-v1-` strings are
documentation placeholders from `REPRODUCTION.md` examples. The real API key's
prefix appears nowhere, and `.env` has never been committed or read into a
transcript in full.

## 2. Agent trajectories — per-run `_meta.json`

The pipeline records its own trajectory on every run: each LLM call, each
decision applied or rejected, each verifier intervention, plus tokens, cost and
per-case runtime. These are machine-readable and already committed.

| File | Run | Trajectory content |
|---|---|---|
| `../outputs/solution/_meta.json` | agent-v1, seed-42 (**primary**) | 14 cases, 1 LLM call, 0 verifier corrections |
| `../outputs/ablation-llm-cause/_meta.json` | rejected ablation (**must-read**) | 12 LLM calls, 23 candidates classified, **4 verifier corrections**, per-candidate deterministic-vs-LLM comparison |
| `../outputs/baseline/_meta.json` | baseline, seed-42 | 14 LLM calls, per-case tokens/cost, the `case_09` outlier (100,806 reasoning tokens) |
| `../outputs/holdout-solution/_meta.json` | agent-v1, seed-777 | 14 cases, 1 LLM call |
| `../outputs/holdout-baseline/_meta.json` | baseline, seed-777 | 14 LLM calls |

### The representative trajectory

**`outputs/ablation-llm-cause/_meta.json`** is the one to read. It is the only
run where the LLM was given authority over a decision the arithmetic could make,
so it is the only run where the verifier had to intervene — and it records both
sides of every disagreement.

Each case entry carries:

- `deterministic_classification` — what the arithmetic decided, recorded
  **before** the LLM overwrote it
- `llm_decisions_applied` — per candidate: `deterministic_type`, `llm_type`,
  `accepted_type`, `agrees_with_deterministic`
- `verifier_corrections` — every drop and every arithmetic correction, with
  claimed vs recomputed values

The four interventions, readable directly from the file:

```
case_12  DROPPED               compound_component_set_unsupported
case_13  CORRECTED_ARITHMETIC  amount_a
case_13  CORRECTED_ARITHMETIC  amount_b
case_13  CORRECTED_ARITHMETIC  difference
```

`case_12` is the instructive one: the model returned `component_causes` as prose
("FEE_MISMATCH: fee A 2.50 vs B 3.35 (delta -0.85) — unexplained discrepancy
beyond rounding") rather than bare enum values. The taxonomy filter stripped
both, leaving an empty set, and the verifier dropped the break — a lost true
positive caused by output formatting, not by faulty reasoning. On `case_13`,
relabelling a COMPOUND as `ROUNDING_DIFFERENCE` changed which amounts
`_recompute()` reports, and the verifier restored the correct figures. Without
it, the numbers in that report would have been wrong rather than merely
mislabelled.

Agreement overall: **19 of 23 candidates (82.6%)**. All four divergences are the
same error — failing to attribute a one-cent gross gap to FX rounding-mode
divergence on an identical foreign amount and rate.

## Reading the transcripts

```bash
# prompts only
python -c "
import json
for l in open('trajectories/session-main-m1-to-submission.jsonl'):
    d=json.loads(l)
    if d.get('type')=='user' and isinstance(d.get('message',{}).get('content'),str):
        print(d['message']['content'][:200], '\n---')
"

# the ablation's verifier interventions
python -c "
import json
m=json.load(open('outputs/ablation-llm-cause/_meta.json'))
for c in m['cases']:
    for v in c['verifier_corrections']:
        print(c['case_id'], v['action'], v['reason'])
"
```
