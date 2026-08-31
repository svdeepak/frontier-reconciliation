# Reproduction guide

Two independent paths:

| | What it verifies | Credentials | Cost |
|---|---|---|---|
| **A. Credential-free** | the whole pipeline, all 113 tests, deterministic data generation and scoring | none | none |
| **B. Real LLM** | the published baseline / agent-v1 numbers | OpenRouter API key | variable, real money |

**Start with A.** It exercises every code path except the provider call and is
enough to verify the implementation, the datasets and the scorer. Path B is only
needed to re-measure the LLM results, which are already committed as artifacts.

---

## 1. Prerequisites

- **Python 3.11+** (developed and measured on 3.14.6; the code uses `X | Y` type
  syntax and `Decimal`-based money, no OS-specific calls)
- `make`
- `git`
- No database, no services, no network for path A

## 2. Setup

```bash
git clone <repo-url> frontier-reconciliation
cd frontier-reconciliation

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

**The virtualenv must be activated** for the rest of this guide. The `Makefile`
invokes bare `python`, which resolves to the venv interpreter only while the
venv is active. Without activation, `make test` may run against a system Python
that lacks the pinned dependencies.

## 3. Install pinned dependencies

```bash
pip install -r requirements.txt
```

Exactly three, all pinned: `jsonschema==4.23.0`, `openpyxl==3.1.5`,
`pytest==8.3.4`. Everything else is standard library.

## 4. Optional `.env` configuration

**Not needed for path A.** `MockProvider` is the default, so the pipeline runs
offline with no configuration at all.

For path B only:

```bash
cp .env.example .env
```

then edit:

```
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=minimax/minimax-m2.7
LLM_PROVIDER=openrouter
```

`.env` is gitignored and has never been committed. Ollama is also supported
(`LLM_PROVIDER=ollama`, `OLLAMA_MODEL=...`) but no reported result used it.

---

## A. Credential-free verification

```bash
make test     # 113 tests, no network, no credentials
make data     # regenerate the 14 seed-42 cases -> data/
```

**Expected:**

```
113 passed
generated 14 cases -> data/ (seed=42, gen v1.0)
```

`make data` is idempotent and writes over `data/` with byte-identical content.
Confirm the committed datasets are exactly what the generator produces:

```bash
PYTHONPATH=src python -m recon.generate --seed 42 --out /tmp/regen
diff -r data /tmp/regen && echo "byte-identical"
```

**Re-score the committed real runs — no API calls.** The evaluator is
deterministic and LLM-free, so it reproduces the published metrics from the
committed outputs:

```bash
PYTHONPATH=src python -m recon.evaluate --outputs outputs/baseline --data data --out /tmp/b.json
PYTHONPATH=src python -m recon.evaluate --outputs outputs/solution --data data --out /tmp/s.json
```

Expected, matching `results/baseline.json` and `results/solution.json`:

```
baseline-v1: P=0.8571 R=0.7826 F1=0.8182 cause_acc=0.7778 ev_valid=1.0 clean_FP=0
agent-v1:    P=1.0    R=1.0    F1=1.0    cause_acc=1.0    ev_valid=1.0 clean_FP=0
```

This is the strongest verification available without spending anything: it
confirms the published numbers follow from the committed raw outputs under the
frozen scorer.

**Run the pipeline end to end on MockProvider:**

```bash
LLM_PROVIDER=mock make solve      # -> outputs/solution/ (OVERWRITES the real run)
make report                       # -> reports/<case_id>.xlsx
```

⚠ `make solve` writes to `outputs/solution/`, overwriting the committed real
agent-v1 run in your working tree. Restore it with
`git checkout outputs/solution`. To avoid this, write elsewhere:

```bash
LLM_PROVIDER=mock PYTHONPATH=src python -m recon.agent --data data --outputs /tmp/mock-solution
```

**MockProvider results are not a measurement.** Canned responses make the
deterministic path score F1 1.0 with no model involved. Never quote a mock run
as a result.

---

## B. Real LLM reproduction

Requires `.env` from §4. **Incurs real API cost, and the amounts below are what
we measured once — not a quote.**

```bash
make baseline    # 14 LLM calls
make solve       # 1 LLM call on this dataset
make eval        # scores both + writes results/comparison.json
make report      # XLSX from outputs/solution/
```

### Measured resource usage (seed-42, `minimax/minimax-m2.7` via OpenRouter)

| | Baseline | agent-v1 |
|---|---|---|
| LLM calls | 14 | 1 |
| Runtime | ~822s (measured) | ~14s (measured) |
| Cost | ~$0.281 (measured) | ~$0.00088 (measured) |

**Labelled as measured values from a single run. Not guaranteed runtime or
pricing** — token usage, provider latency, rate limits and per-model pricing all
vary, and reasoning-token spend on this model was highly variable.

**The baseline has a large runtime/cost outlier.** One case,
`case_09_rounding` — two one-cent deltas at the tolerance boundary — consumed
**100,806 reasoning tokens, 420.34s, and $0.2439**: 51% of baseline wall clock
and 87% of baseline cost, for a correct answer. The median baseline case took
~28s and ~$0.0028. Expect high variance, and budget for a case that spirals. The
same case did not spiral on the seed-777 holdout, where the baseline totalled
566.97s / $0.0569 — so this is run-to-run variance, not a fixed property.
Detail: [docs/BASELINE_RUN_NOTES.md](docs/BASELINE_RUN_NOTES.md) §2.

`make eval` chains three commands and needs **both** `outputs/baseline/` and
`outputs/solution/` populated; run `make baseline` and `make solve` first.

Provider failures surface as `ProviderError` with the HTTP status and the
provider's own message. There are no retries and no fallback models — a failed
run fails visibly rather than being recorded as an empty result.

---

## C. Reproducing the committed artifacts

Everything published is committed, so these are verifications rather than
re-runs:

| Artifact | Committed at | Reproduce with |
|---|---|---|
| seed-42 datasets | `data/` | `make data` (byte-identical) |
| Baseline raw outputs | `outputs/baseline/` | path B `make baseline` (new LLM run) |
| agent-v1 raw outputs | `outputs/solution/` | path B `make solve` (new LLM run) |
| Scored metrics | `results/*.json` | path A evaluator re-score (no cost) |
| Sample XLSX | `docs/sample_exception_report_case13.xlsx` | `make report` (writes all 14 to `reports/`) |

`reports/` is gitignored; `make report` regenerates all 14 workbooks there from
the committed `outputs/solution/`. The committed sample is the `case_13` one,
copied to `docs/` under an obvious name.

## D. Held-out experiment (seed 777) — already complete

**Not required for normal verification.** The holdout is finished and its
artifacts are committed: `data-holdout/`, `outputs/holdout-baseline/`,
`outputs/holdout-solution/`, `results/holdout-*.json`. Re-scoring them costs
nothing:

```bash
PYTHONPATH=src python -m recon.evaluate --outputs outputs/holdout-solution \
  --data data-holdout --out /tmp/h.json      # expect F1 1.0
PYTHONPATH=src python -m recon.evaluate --outputs outputs/holdout-baseline \
  --data data-holdout --out /tmp/hb.json     # expect F1 0.72
```

Re-running the holdout's LLM calls is only needed to re-measure, and costs money:

```bash
PYTHONPATH=src python -m recon.generate --seed 777 --out data-holdout
LLM_PROVIDER=openrouter OPENROUTER_MODEL=minimax/minimax-m2.7 \
  PYTHONPATH=src python -m recon.baseline --data data-holdout --outputs outputs/holdout-baseline
LLM_PROVIDER=openrouter OPENROUTER_MODEL=minimax/minimax-m2.7 \
  PYTHONPATH=src python -m recon.agent --data data-holdout --outputs outputs/holdout-solution
```

Findings and limitations: [docs/HOLDOUT_SEED777.md](docs/HOLDOUT_SEED777.md).

## E. Rejected ablation — opt-in only

The LLM-cause-classification ablation is **not** part of the default design and
is gated behind an explicit flag, so it cannot run by accident:

```bash
LLM_CAUSE_ABLATION=1 LLM_PROVIDER=openrouter OPENROUTER_MODEL=minimax/minimax-m2.7 \
  PYTHONPATH=src python -m recon.ablation_llm_cause --data data --outputs outputs/ablation-llm-cause
```

It was rejected: F1 0.9778 vs 1.0, cause accuracy 0.8636 vs 1.0, at ×12 LLM
calls and ×14.9 cost. Results are committed; re-running costs money and changes
nothing. Detail: [docs/ABLATION_LLM_CAUSE.md](docs/ABLATION_LLM_CAUSE.md).

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ModuleNotFoundError: recon` | `PYTHONPATH=src` missing — the `make` targets set it; direct `python -m` calls need it |
| `ModuleNotFoundError: jsonschema` / `openpyxl` | venv not activated, or `pip install -r requirements.txt` not run |
| `OPENROUTER_API_KEY and OPENROUTER_MODEL required` | `LLM_PROVIDER=openrouter` with an unset key or model — see §4 |
| Pipeline runs but every result is suspiciously perfect | `LLM_PROVIDER=mock` (the default). Mock is not a measurement |
| `make eval` fails on the second command | `outputs/solution/` not populated — run `make solve` first |
| `ProviderError ... HTTP 429` | OpenRouter rate limit. No retries by design; wait and re-run |
