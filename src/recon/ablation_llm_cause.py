"""ABLATION EXPERIMENT — LLM cause classification (secondary, not agent-v1).

Research question: does routing cause classification through the LLM, instead of
deciding it by arithmetic, change accuracy, verifier corrections, latency, tokens
or cost?

This module is an isolated experimental variant. It imports agent-v1 and does not
modify it: `recon.agent` is untouched, so default behaviour is unaffected whether
or not this module is ever loaded.

CONTROL (agent-v1, `recon.agent`):
    normalize -> match -> DETERMINISTIC cause classification -> LLM for AMBIGUOUS
    only -> verifier -> output

ABLATION (this module):
    normalize -> match -> candidates built the same way, then the cause of EVERY
    candidate is decided by the LLM -> verifier -> output

Identical to the control, by reuse rather than reimplementation:
  * normalization, matching and candidate construction  (agent.normalize,
    agent.match_candidates, agent.build_candidates)
  * every computed amount: row sets, amount_a/amount_b/difference are the
    deterministic values; the LLM is never asked for a number
  * the LLM system prompt (agent.LLM_SYSTEM_PROMPT), reply parsing
    (agent.strip_fences), taxonomy gate (agent.ALLOWED_TYPES) and provider
  * the verifier, unchanged and still downstream (agent.verify)
  * the `matches` array, still decided by deterministic arithmetic — the
    ablation changes cause labelling, not what counts as a clean pair
  * output schema, evaluator and scoring semantics

The only difference: which component chooses `break_type` (and, for COMPOUND,
`component_causes`).

Two deliberate design decisions, both to keep the experiment honest:

1. The LLM is NOT shown the deterministic answer. `agent._candidate_brief`
   includes `deterministic_break_type`, which would hand the model the very
   label under test, so this module builds its own brief that withholds it while
   keeping every other field (rows, computed amounts, computed evidence). The
   candidate's real structural context is supplied instead — how many rows on
   each side, and whether each side is present — so the model is informed, not
   misled.
2. Candidates are NOT relabelled `AMBIGUOUS` to slip past
   `agent.apply_decisions`'s gate (which accepts an LLM label only for
   AMBIGUOUS candidates). This module implements its own small merge,
   `apply_cause_decisions`, so nothing fabricates a type the data does not have.

Enable with LLM_CAUSE_ABLATION=1 (guard, so the module cannot be run by
accident) plus the usual provider environment.

Usage:
  LLM_CAUSE_ABLATION=1 LLM_PROVIDER=openrouter OPENROUTER_MODEL=... \
    python -m recon.ablation_llm_cause --data data --outputs outputs/ablation-llm-cause
"""

import argparse
import json
import os
import time
from pathlib import Path

from recon import agent
from recon.providers import provider_from_env
from recon.taxonomy import SCHEMA_VERSION

SYSTEM_NAME = "agent-v1-ablation-llm-cause"
ENV_FLAG = "LLM_CAUSE_ABLATION"


def _ablation_brief(idx: int, c: agent.Candidate, a_by_id: dict, b_by_id: dict) -> dict:
    """Candidate brief WITHOUT the deterministic cause.

    Mirrors agent._candidate_brief field for field, except that
    `deterministic_break_type` is withheld — that label is the variable under
    test. Structural context is given in its place so the model has the same
    facts a human analyst would, minus the answer.
    """
    def describe(rid, src):
        r = (a_by_id if src == "A" else b_by_id)[rid]
        return {"row_id": r.row_id, "date": r.date, "reference": r.reference,
                "currency": r.currency, "gross_amount": str(r.gross_amount),
                "fee_amount": str(r.fee_amount), "net_amount": str(r.net_amount),
                "fx_rate": None if r.fx_rate is None else str(r.fx_rate),
                "foreign_currency": r.foreign_currency,
                "foreign_amount": None if r.foreign_amount is None else str(r.foreign_amount)}
    return {
        "break_id": f"C-{idx:03d}",
        "structure": {"n_a_rows": len(c.a_ids), "n_b_rows": len(c.b_ids),
                      "present_in_a": bool(c.a_ids), "present_in_b": bool(c.b_ids)},
        "computed_amount_a": None if c.amount_a is None else str(c.amount_a),
        "computed_amount_b": None if c.amount_b is None else str(c.amount_b),
        "computed_difference": None if c.difference is None else str(c.difference),
        "a_rows": [describe(r, "A") for r in c.a_ids],
        "b_rows": [describe(r, "B") for r in c.b_ids],
        "computed_evidence": c.evidence,
    }


def llm_classify_all(provider, case_id: str, candidates: list[agent.Candidate],
                     a_by_id: dict, b_by_id: dict) -> tuple[dict, dict]:
    """Ask the LLM for the cause of EVERY candidate. One call per case.

    Reuses agent.LLM_SYSTEM_PROMPT and agent.strip_fences unchanged. Failure
    handling matches the control: a malformed reply or transport error degrades
    to whatever the candidates already carry, and is recorded in metadata.
    """
    meta = {"called": False, "n_candidates": len(candidates), "seconds": 0.0,
            "usage": {}, "error": None}
    if not candidates:
        return {}, meta

    briefs = [_ablation_brief(i, c, a_by_id, b_by_id)
              for i, c in enumerate(candidates, 1)]
    user = (f"case_id: {case_id}\n\nClassify the cause of every candidate "
            f"exception below. The amounts are already computed and "
            f"authoritative; decide only the break_type (and component_causes "
            f"for COMPOUND).\n" + json.dumps(briefs, indent=2))
    t0 = time.time()
    meta["called"] = True
    try:
        reply = provider.complete(agent.LLM_SYSTEM_PROMPT, user)
        parsed = json.loads(agent.strip_fences(reply))
        decisions = {d["break_id"]: d for d in parsed.get("decisions", [])
                     if isinstance(d, dict) and "break_id" in d}
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        decisions = {}
    except Exception as e:  # provider/transport failure must not abort the run
        meta["error"] = f"{type(e).__name__}: {e}"
        decisions = {}
    meta["seconds"] = round(time.time() - t0, 2)
    meta["usage"] = dict(getattr(provider, "last_usage", {}) or {})
    return decisions, meta


def apply_cause_decisions(candidates: list[agent.Candidate], decisions: dict) -> list[dict]:
    """Minimal merge for the ablation: accept the LLM's cause for any candidate.

    Distinct from agent.apply_decisions, which accepts a relabel only for
    AMBIGUOUS candidates. Arithmetic fields are never touched here either — only
    break_type, component_causes, confidence and prose — so the verifier
    downstream still owns every number.

    Records what was accepted or rejected, and what the deterministic classifier
    would have said, so the two can be compared after the fact.
    """
    applied: list[dict] = []
    for i, c in enumerate(candidates, 1):
        cid = f"C-{i:03d}"
        d = decisions.get(cid)
        note = {"break_id": cid,
                "deterministic_type": c.break_type,
                "deterministic_components": sorted(c.component_causes),
                "llm_type": None if not d else d.get("break_type"),
                # `or []` not a dict default: the model sometimes sends an
                # explicit `"component_causes": null`, which .get() returns as
                # None rather than falling back.
                "llm_components": sorted(d.get("component_causes") or []) if d else [],
                "accepted_type": False,
                "agrees_with_deterministic": None}
        if d:
            t = d.get("break_type")
            if t in agent.ALLOWED_TYPES:
                same_type = t == c.break_type
                c.break_type = t
                if t == "COMPOUND":
                    comps = [x for x in (d.get("component_causes") or [])
                             if x in agent.ALLOWED_TYPES]
                    c.component_causes = sorted(set(comps))
                else:
                    c.component_causes = []
                note["accepted_type"] = True
                note["agrees_with_deterministic"] = (
                    same_type and set(note["llm_components"]) == set(note["deterministic_components"])
                    if t == "COMPOUND" else same_type)
            conf = d.get("confidence")
            if conf in agent.CONFIDENCES:
                c.confidence = conf
            if isinstance(d.get("rationale"), str) and d["rationale"].strip():
                c.llm_note = d["rationale"].strip()
            if isinstance(d.get("suggested_action"), str) and d["suggested_action"].strip():
                c.suggested_action_override = d["suggested_action"].strip()
        applied.append(note)
    return applied


def solve_case(provider, data_dir: Path, case_id: str) -> tuple[dict, dict]:
    """Ablation variant of agent.solve_case. Stages 1-3 and 5 are agent-v1's."""
    case_dir = data_dir / "cases" / case_id
    t0 = time.time()

    a_rows = agent.normalize(case_dir / "source_a.csv")
    b_rows = agent.normalize(case_dir / "source_b.csv")
    a_by_id = {r.row_id: r for r in a_rows}
    b_by_id = {r.row_id: r for r in b_rows}

    m = agent.match_candidates(a_rows, b_rows)
    candidates = agent.build_candidates(m)

    # Record what the deterministic classifier decided BEFORE the LLM overwrites
    # it, so agreement can be measured per candidate.
    deterministic = [{"break_id": f"C-{i:03d}", "break_type": c.break_type,
                      "component_causes": sorted(c.component_causes)}
                     for i, c in enumerate(candidates, 1)]

    decisions, llm_meta = llm_classify_all(provider, case_id, candidates,
                                           a_by_id, b_by_id)
    applied = apply_cause_decisions(candidates, decisions)

    # Verifier: unchanged, still downstream, still owns every number.
    v = agent.verify(candidates, a_by_id, b_by_id)

    # `matches` still decided by deterministic arithmetic, exactly as agent-v1 —
    # the ablation varies cause labelling only.
    matched_pairs = [{"a_row_id": a.row_id, "b_row_id": b.row_id}
                     for a, b in m.pairs if agent.classify_pair(a, b) is None]

    output = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "system": {"name": SYSTEM_NAME,
                   "model": getattr(provider, "model", provider.name),
                   "provider": provider.name},
        "matches": sorted(matched_pairs, key=lambda x: x["a_row_id"]),
        "breaks": v.breaks,
    }
    n_agree = sum(1 for x in applied if x["agrees_with_deterministic"] is True)
    n_accepted = sum(1 for x in applied if x["accepted_type"])
    meta = {
        "case_id": case_id,
        "seconds": round(time.time() - t0, 2),
        "rows_a": len(a_rows),
        "rows_b": len(b_rows),
        "n_pairs": len(m.pairs),
        "n_candidates": len(candidates),
        "n_candidates_sent_to_llm": llm_meta["n_candidates"],
        "n_breaks_emitted": len(v.breaks),
        "llm": llm_meta,
        "deterministic_classification": deterministic,
        "llm_decisions_applied": applied,
        "n_llm_types_accepted": n_accepted,
        "n_llm_agrees_with_deterministic": n_agree,
        "verifier_corrections": v.corrections,
        "n_verifier_corrections": v.n_corrections,
    }
    return output, meta


def run(data_dir: Path, outputs_dir: Path) -> dict:
    provider = provider_from_env()
    outputs_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    meta = {"system": SYSTEM_NAME,
            "experiment": "ablation: LLM cause classification replaces deterministic",
            "control_system": agent.SYSTEM_NAME,
            "provider": provider.name,
            "model": getattr(provider, "model", provider.name),
            "data_master_seed": manifest["master_seed"],
            "cases": []}
    t_start = time.time()

    for c in sorted(manifest["cases"], key=lambda x: x["case_id"]):
        cid = c["case_id"]
        output, case_meta = solve_case(provider, data_dir, cid)
        (outputs_dir / f"{cid}.json").write_text(
            json.dumps(output, indent=2, sort_keys=True) + "\n")
        meta["cases"].append(case_meta)

    meta["total_seconds"] = round(time.time() - t_start, 2)
    meta["total_tokens"] = {
        "prompt": sum(x["llm"]["usage"].get("prompt_tokens") or 0 for x in meta["cases"]),
        "completion": sum(x["llm"]["usage"].get("completion_tokens") or 0 for x in meta["cases"]),
    }
    meta["total_reasoning_tokens"] = sum(
        ((x["llm"]["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        for x in meta["cases"])
    meta["total_cost_usd"] = round(sum(
        x["llm"]["usage"].get("cost") or 0 for x in meta["cases"]), 8)
    meta["n_llm_calls"] = sum(1 for x in meta["cases"] if x["llm"]["called"])
    meta["n_candidates_sent_to_llm"] = sum(x["n_candidates_sent_to_llm"] for x in meta["cases"])
    meta["total_verifier_corrections"] = sum(x["n_verifier_corrections"] for x in meta["cases"])
    meta["n_llm_types_accepted"] = sum(x["n_llm_types_accepted"] for x in meta["cases"])
    meta["n_llm_agrees_with_deterministic"] = sum(
        x["n_llm_agrees_with_deterministic"] for x in meta["cases"])
    (outputs_dir / "_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return meta


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="ABLATION: LLM cause classification")
    p.add_argument("--data", type=str, default="data")
    p.add_argument("--outputs", type=str, default="outputs/ablation-llm-cause")
    args = p.parse_args()
    if os.environ.get(ENV_FLAG) != "1":
        raise SystemExit(
            f"{ENV_FLAG}=1 required. This is an ablation experiment, not agent-v1; "
            f"it is never part of a normal run.")
    m = run(Path(args.data), Path(args.outputs))
    print(f"ABLATION complete: system={m['system']} provider={m['provider']} "
          f"model={m['model']} cases={len(m['cases'])} "
          f"llm_calls={m['n_llm_calls']} candidates_to_llm={m['n_candidates_sent_to_llm']} "
          f"llm_agrees_with_deterministic={m['n_llm_agrees_with_deterministic']}"
          f"/{m['n_llm_types_accepted']} "
          f"verifier_corrections={m['total_verifier_corrections']} "
          f"seconds={m['total_seconds']} cost=${m['total_cost_usd']}")
