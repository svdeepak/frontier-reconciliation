"""One-prompt baseline (M4). This is the experimental CONTROL.

For each committed case: send both CSVs and the output schema in a single
general-purpose prompt, parse the reply, write outputs/baseline/<case_id>.json.

Deliberately simple, per the frozen contract and brief §10:
- one prompt, one model call per case;
- no multi-stage workflow, no tools, no verification pass, no retries;
- the only post-processing is code-fence stripping and JSON parsing —
  the minimum required to produce a file at all. Unparseable or
  schema-invalid replies are written as-is inside an envelope and are
  scored honestly by the evaluator (all ground-truth breaks become FN).

Never optimize this after seeing results.

Usage: python -m recon.baseline [--data data] [--outputs outputs/baseline]
Provider comes from environment (LLM_PROVIDER / see .env.example).
"""

import argparse
import json
import time
from pathlib import Path

from recon.providers import provider_from_env

SYSTEM_PROMPT = """You are a payments reconciliation assistant. You will receive two \
transaction lists as CSV: Source A (internal ledger) and Source B (partner statement). \
Reconcile them and report every discrepancy ("break").

Break types: MISSING_IN_A, MISSING_IN_B, AMOUNT_MISMATCH, FEE_MISMATCH, FX_DIFFERENCE, \
ROUNDING_DIFFERENCE, DUPLICATE, AMBIGUOUS, COMPOUND (two or more causes on the same \
correspondence; list them in component_causes).

Respond with ONLY a JSON object, no markdown fences, no commentary, exactly this shape:
{
  "schema_version": "1.0",
  "case_id": "<case id>",
  "system": {"name": "baseline-v1", "model": "<model>", "provider": "<provider>"},
  "matches": [{"a_row_id": "...", "b_row_id": "..."}],
  "breaks": [
    {
      "break_id": "P-001",
      "break_type": "...",
      "component_causes": ["..."],
      "a_row_ids": ["..."],
      "b_row_ids": ["..."],
      "amount_a": 0.0,
      "amount_b": 0.0,
      "difference": 0.0,
      "evidence": "calculation citing the row ids and amounts",
      "suggested_action": "...",
      "confidence": "HIGH" | "MEDIUM" | "LOW"
    }
  ]
}
Omit component_causes unless break_type is COMPOUND. Use only row_id values that \
exist in the provided data."""


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def run(data_dir: Path, outputs_dir: Path) -> dict:
    provider = provider_from_env()
    model = getattr(provider, "model", "mock")
    outputs_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    meta = {"provider": provider.name, "model": model, "cases": [], "parse_failures": []}
    t_start = time.time()

    for c in sorted(manifest["cases"], key=lambda x: x["case_id"]):
        cid = c["case_id"]
        case_dir = data_dir / "cases" / cid
        user = (f"case_id: {cid}\n\n=== SOURCE A (internal ledger) ===\n"
                f"{(case_dir / 'source_a.csv').read_text()}\n"
                f"=== SOURCE B (partner statement) ===\n"
                f"{(case_dir / 'source_b.csv').read_text()}")
        t0 = time.time()
        reply = provider.complete(SYSTEM_PROMPT, user)
        elapsed = round(time.time() - t0, 2)
        try:
            out = json.loads(strip_fences(reply))
            out.setdefault("schema_version", "1.0")
            out.setdefault("case_id", cid)
            out.setdefault("system", {})
            out["system"] = {"name": "baseline-v1", "model": model, "provider": provider.name,
                             **out.get("system", {})}
            out["system"]["name"] = "baseline-v1"
        except json.JSONDecodeError as e:
            meta["parse_failures"].append({"case_id": cid, "error": str(e),
                                           "raw_reply": reply[:2000]})
            out = {"schema_version": "1.0", "case_id": cid,
                   "system": {"name": "baseline-v1", "model": model, "provider": provider.name},
                   "breaks": []}
        (outputs_dir / f"{cid}.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        meta["cases"].append({"case_id": cid, "seconds": elapsed,
                              "usage": dict(getattr(provider, "last_usage", {}) or {})})

    meta["total_seconds"] = round(time.time() - t_start, 2)
    pt = sum(x["usage"].get("prompt_tokens") or 0 for x in meta["cases"])
    ct = sum(x["usage"].get("completion_tokens") or 0 for x in meta["cases"])
    meta["total_tokens"] = {"prompt": pt, "completion": ct}
    (outputs_dir / "_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return meta


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=str, default="data")
    p.add_argument("--outputs", type=str, default="outputs/baseline")
    args = p.parse_args()
    m = run(Path(args.data), Path(args.outputs))
    print(f"baseline complete: provider={m['provider']} model={m['model']} "
          f"cases={len(m['cases'])} parse_failures={len(m['parse_failures'])} "
          f"total_seconds={m['total_seconds']}")
