"""Deterministic evaluator (M3).

Scores system outputs (agent_output schema) against committed ground truth.
LLM-free, credential-free, deterministic: same inputs -> byte-identical
results JSON.

Scoring semantics are frozen in docs/EVALUATION.md:
- Detection TP requires EXACT row-set identity on both sides (A and B).
  DUPLICATE and COMPOUND get no partial credit: the complete row set is
  required. Matching is one-to-one; a second prediction with the same row
  sets is an FP (duplicate prediction).
- Cause is scored over TPs only. COMPOUND requires the full component-cause
  set. Wrong cause does not revoke detection credit.
- A prediction referencing any nonexistent row ID (in a_row_ids/b_row_ids)
  is an FP regardless of other fields, and counts as evidence-invalid.
  Evidence validity is defined over the row-ID arrays, not free text.

Per-prediction diagnostic categories:
  TP                       exact rows, correct cause
  TP_WRONG_CAUSE           exact rows, wrong cause (incl. incomplete COMPOUND set)
  FP_HALLUCINATED_ROW_ID   references a row ID absent from the sources
  FP_DUPLICATE_PREDICTION  exact rows of a GT break already claimed by another prediction
  FP_PARTIAL_OVERLAP       overlaps a GT break's rows but row sets not exact
  FP_NO_OVERLAP            shares no rows with any GT break
Per-GT category: matched (by prediction id) or MISSED.

Usage:
  python -m recon.evaluate --outputs outputs/baseline --data data --out results/baseline.json
  python -m recon.evaluate --compare results/baseline.json results/solution.json --out results/comparison.json
"""

import argparse
import csv
import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
OUT_SCHEMA = json.loads((ROOT / "schemas" / "agent_output.schema.json").read_text())
CLEAN_CASE_PREFIXES = ("case_01", "case_02")


def _round(x: float) -> float:
    return round(x, 4)


def load_case(data_dir: Path, case_id: str):
    d = data_dir / "cases" / case_id
    gt = json.loads((d / "ground_truth.json").read_text())
    row_ids = set()
    for src in ("source_a.csv", "source_b.csv"):
        with open(d / src) as f:
            for r in csv.DictReader(f):
                row_ids.add(r["row_id"])
    return gt, row_ids


def load_output(outputs_dir: Path, case_id: str):
    """Returns (output_dict|None, schema_valid, error)."""
    p = outputs_dir / f"{case_id}.json"
    if not p.exists():
        return None, False, "missing output file"
    try:
        out = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        return None, False, f"invalid JSON: {e}"
    try:
        jsonschema.validate(out, OUT_SCHEMA)
    except jsonschema.ValidationError as e:
        return out, False, f"schema violation: {e.message}"
    return out, True, None


def _rowsets(item) -> tuple[frozenset, frozenset]:
    return frozenset(item["a_row_ids"]), frozenset(item["b_row_ids"])


def _cause_correct(pred, gt) -> bool:
    if pred["break_type"] != gt["break_type"]:
        return False
    if gt["break_type"] == "COMPOUND":
        return set(pred.get("component_causes", [])) == set(gt.get("component_causes", []))
    return True


def score_case(case_id: str, gt: dict, row_ids: set, output, schema_valid: bool, error):
    gt_breaks = sorted(gt["breaks"], key=lambda b: b["break_id"])
    diag = {
        "case_id": case_id,
        "schema_valid": schema_valid,
        "error": error,
        "n_gt_breaks": len(gt_breaks),
        "n_predictions": 0,
        "tp": 0, "fp": 0, "fn": 0,
        "tp_correct_cause": 0,
        "predictions": [],
        "missed_break_ids": [],
        "false_positives": [],
        "cause_errors": [],
        "invalid_evidence": [],
    }
    preds = []
    if output is not None and schema_valid:
        preds = sorted(output.get("breaks", []), key=lambda b: b["break_id"])
    diag["n_predictions"] = len(preds)

    gt_by_rows = {_rowsets(g): g for g in gt_breaks}
    claimed: dict[str, str] = {}  # gt break_id -> pred break_id
    all_gt_rows = set()
    for g in gt_breaks:
        all_gt_rows |= set(g["a_row_ids"]) | set(g["b_row_ids"])

    for p in preds:
        pid = p["break_id"]
        cited = set(p["a_row_ids"]) | set(p["b_row_ids"])
        hallucinated = sorted(cited - row_ids)
        if hallucinated:
            diag["invalid_evidence"].append({"break_id": pid, "nonexistent_row_ids": hallucinated})
            cat = "FP_HALLUCINATED_ROW_ID"
        else:
            g = gt_by_rows.get(_rowsets(p))
            if g is not None:
                if g["break_id"] in claimed:
                    cat = "FP_DUPLICATE_PREDICTION"
                else:
                    claimed[g["break_id"]] = pid
                    correct_cause = _cause_correct(p, g)
                    cat = "TP" if correct_cause else "TP_WRONG_CAUSE"
                    if not correct_cause:
                        diag["cause_errors"].append({
                            "break_id": pid,
                            "gt_break_id": g["break_id"],
                            "predicted": p["break_type"],
                            "predicted_components": sorted(p.get("component_causes", [])),
                            "expected": g["break_type"],
                            "expected_components": sorted(g.get("component_causes", [])),
                        })
            elif cited & all_gt_rows:
                cat = "FP_PARTIAL_OVERLAP"
            else:
                cat = "FP_NO_OVERLAP"
        diag["predictions"].append({
            "break_id": pid, "category": cat,
            "a_row_ids": sorted(p["a_row_ids"]), "b_row_ids": sorted(p["b_row_ids"]),
        })
        if cat in ("TP", "TP_WRONG_CAUSE"):
            diag["tp"] += 1
            if cat == "TP":
                diag["tp_correct_cause"] += 1
        else:
            diag["fp"] += 1
            diag["false_positives"].append({"break_id": pid, "category": cat,
                                            "a_row_ids": sorted(p["a_row_ids"]),
                                            "b_row_ids": sorted(p["b_row_ids"])})

    for g in gt_breaks:
        if g["break_id"] not in claimed:
            diag["fn"] += 1
            diag["missed_break_ids"].append(g["break_id"])
    return diag


def score_outputs(outputs_dir: Path, data_dir: Path, system_name: str | None = None) -> dict:
    manifest = json.loads((data_dir / "manifest.json").read_text())
    case_ids = sorted(c["case_id"] for c in manifest["cases"])
    per_case, system = [], None
    for cid in case_ids:
        gt, row_ids = load_case(data_dir, cid)
        output, ok, err = load_output(outputs_dir, cid)
        if output and system is None:
            system = output.get("system")
        per_case.append(score_case(cid, gt, row_ids, output, ok, err))

    tp = sum(c["tp"] for c in per_case)
    fp = sum(c["fp"] for c in per_case)
    fn = sum(c["fn"] for c in per_case)
    n_preds = sum(c["n_predictions"] for c in per_case)
    invalid_ev = sum(len(c["invalid_evidence"]) for c in per_case)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    cause_acc = (sum(c["tp_correct_cause"] for c in per_case) / tp) if tp else 0.0
    clean_fp = sum(c["fp"] for c in per_case if c["case_id"].startswith(CLEAN_CASE_PREFIXES))

    return {
        "schema_version": "1.0",
        "system": system or {"name": system_name or outputs_dir.name, "model": "unknown"},
        "data_master_seed": manifest["master_seed"],
        "n_cases": len(case_ids),
        "totals": {
            "tp": tp, "fp": fp, "fn": fn,
            "n_predictions": n_preds,
            "precision": _round(precision),
            "recall": _round(recall),
            "f1": _round(f1),
            "cause_accuracy_over_tp": _round(cause_acc),
            "evidence_validity_rate": _round(1 - invalid_ev / n_preds) if n_preds else 1.0,
            "clean_case_false_positives": clean_fp,
        },
        "per_case": per_case,
    }


def compare(baseline: dict, solution: dict) -> dict:
    keys = ["precision", "recall", "f1", "cause_accuracy_over_tp",
            "evidence_validity_rate", "clean_case_false_positives"]
    delta = {}
    for k in keys:
        b, s = baseline["totals"][k], solution["totals"][k]
        delta[k] = _round(s - b) if isinstance(b, float) or isinstance(s, float) else s - b
    return {
        "schema_version": "1.0",
        "baseline": {"system": baseline["system"], "totals": baseline["totals"]},
        "solution": {"system": solution["system"], "totals": solution["totals"]},
        "delta": delta,
        "primary_metric": "f1",
        "primary_delta": delta["f1"],
    }


def write_json(obj: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--outputs", type=str, help="directory of per-case output JSONs")
    p.add_argument("--data", type=str, default="data")
    p.add_argument("--out", type=str, required=True)
    p.add_argument("--system-name", type=str, default=None)
    p.add_argument("--compare", nargs=2, metavar=("BASELINE_JSON", "SOLUTION_JSON"))
    args = p.parse_args()
    if args.compare:
        b = json.loads(Path(args.compare[0]).read_text())
        s = json.loads(Path(args.compare[1]).read_text())
        result = compare(b, s)
    else:
        if not args.outputs:
            raise SystemExit("--outputs required unless --compare is used")
        result = score_outputs(Path(args.outputs), Path(args.data), args.system_name)
        t = result["totals"]
        print(f"{result['system']['name']}: P={t['precision']} R={t['recall']} F1={t['f1']} "
              f"cause_acc={t['cause_accuracy_over_tp']} ev_valid={t['evidence_validity_rate']} "
              f"clean_FP={t['clean_case_false_positives']}")
    write_json(result, Path(args.out))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
