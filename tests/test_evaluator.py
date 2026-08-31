"""M3 evaluator tests — adversarial scoring scenarios against real committed data.

Predictions are handcrafted against case_13_signature_adversarial (3 GT breaks:
COMPOUND GT-001, DUPLICATE GT-002, MISSING_IN_B GT-003) and the clean case_01.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.evaluate import compare, load_case, score_case, score_outputs, write_json  # noqa: E402
from recon.providers import MockProvider  # noqa: E402

DATA = ROOT / "data"
SIG = "case_13_signature_adversarial"


@pytest.fixture(scope="module")
def sig():
    gt, row_ids = load_case(DATA, SIG)
    return gt, row_ids


def pred(break_id, break_type, a_ids, b_ids, components=None):
    p = {
        "break_id": break_id, "break_type": break_type,
        "a_row_ids": a_ids, "b_row_ids": b_ids,
        "evidence": "test evidence", "suggested_action": "review", "confidence": "HIGH",
    }
    if components:
        p["component_causes"] = components
    return p


def output(case_id, breaks):
    return {"schema_version": "1.0", "case_id": case_id,
            "system": {"name": "test", "model": "mock", "provider": "MockProvider"},
            "breaks": breaks}


def gtb(gt, break_type):
    return next(b for b in gt["breaks"] if b["break_type"] == break_type)


def run(gt, row_ids, breaks, valid=True, out=None):
    return score_case(gt["case_id"], gt, row_ids, out or output(gt["case_id"], breaks), valid, None)


def test_exact_correct_prediction(sig):
    gt, row_ids = sig
    g = gtb(gt, "COMPOUND")
    d = run(gt, row_ids, [pred("P1", "COMPOUND", g["a_row_ids"], g["b_row_ids"],
                               g["component_causes"])])
    assert d["tp"] == 1 and d["tp_correct_cause"] == 1 and d["fp"] == 0
    assert d["predictions"][0]["category"] == "TP"
    assert d["fn"] == 2  # other two GT breaks missed


def test_wrong_row_id_is_fp_and_gt_missed(sig):
    gt, row_ids = sig
    g = gtb(gt, "COMPOUND")
    wrong_a = [rid for rid in row_ids if rid.startswith("A") and rid not in g["a_row_ids"]][:1]
    d = run(gt, row_ids, [pred("P1", "COMPOUND", wrong_a, g["b_row_ids"], g["component_causes"])])
    assert d["tp"] == 0 and d["fp"] == 1
    assert d["predictions"][0]["category"] in ("FP_PARTIAL_OVERLAP", "FP_NO_OVERLAP")
    assert gtb(gt, "COMPOUND")["break_id"] in d["missed_break_ids"]


def test_missing_break_counted_fn(sig):
    gt, row_ids = sig
    d = run(gt, row_ids, [])
    assert d["tp"] == 0 and d["fp"] == 0 and d["fn"] == 3
    assert sorted(d["missed_break_ids"]) == sorted(b["break_id"] for b in gt["breaks"])


def test_false_positive_no_overlap(sig):
    gt, row_ids = sig
    gt_rows = set()
    for b in gt["breaks"]:
        gt_rows |= set(b["a_row_ids"]) | set(b["b_row_ids"])
    free_a = sorted(r for r in row_ids if r.startswith("A") and r not in gt_rows)[:1]
    free_b = sorted(r for r in row_ids if r.startswith("B") and r not in gt_rows)[:1]
    d = run(gt, row_ids, [pred("P1", "AMOUNT_MISMATCH", free_a, free_b)])
    assert d["fp"] == 1
    assert d["false_positives"][0]["category"] == "FP_NO_OVERLAP"


def test_correct_detection_wrong_cause(sig):
    gt, row_ids = sig
    g = gtb(gt, "MISSING_IN_B")
    d = run(gt, row_ids, [pred("P1", "AMOUNT_MISMATCH", g["a_row_ids"], g["b_row_ids"])])
    assert d["tp"] == 1 and d["tp_correct_cause"] == 0
    assert d["predictions"][0]["category"] == "TP_WRONG_CAUSE"
    assert d["cause_errors"][0]["expected"] == "MISSING_IN_B"


def test_compound_incomplete_components_is_wrong_cause(sig):
    gt, row_ids = sig
    g = gtb(gt, "COMPOUND")
    d = run(gt, row_ids, [pred("P1", "COMPOUND", g["a_row_ids"], g["b_row_ids"],
                               ["FEE_MISMATCH"])])  # incomplete set
    assert d["tp"] == 1 and d["tp_correct_cause"] == 0
    assert d["cause_errors"][0]["expected_components"] == ["FEE_MISMATCH", "FX_DIFFERENCE"]


def test_duplicate_requires_complete_row_set(sig):
    gt, row_ids = sig
    g = gtb(gt, "DUPLICATE")
    assert len(g["b_row_ids"]) == 2
    d = run(gt, row_ids, [pred("P1", "DUPLICATE", [], g["b_row_ids"][:1])])  # only one of two rows
    assert d["tp"] == 0 and d["fp"] == 1
    assert d["predictions"][0]["category"] == "FP_PARTIAL_OVERLAP"
    assert g["break_id"] in d["missed_break_ids"]


def test_hallucinated_row_id_is_fp_and_invalid_evidence(sig):
    gt, row_ids = sig
    g = gtb(gt, "COMPOUND")
    d = run(gt, row_ids, [pred("P1", "COMPOUND", g["a_row_ids"] + ["A9999"],
                               g["b_row_ids"], g["component_causes"])])
    assert d["tp"] == 0 and d["fp"] == 1
    assert d["predictions"][0]["category"] == "FP_HALLUCINATED_ROW_ID"
    assert d["invalid_evidence"][0]["nonexistent_row_ids"] == ["A9999"]


def test_duplicate_prediction_second_is_fp(sig):
    gt, row_ids = sig
    g = gtb(gt, "COMPOUND")
    p1 = pred("P1", "COMPOUND", g["a_row_ids"], g["b_row_ids"], g["component_causes"])
    p2 = pred("P2", "COMPOUND", g["a_row_ids"], g["b_row_ids"], g["component_causes"])
    d = run(gt, row_ids, [p1, p2])
    assert d["tp"] == 1 and d["fp"] == 1
    assert d["predictions"][1]["category"] == "FP_DUPLICATE_PREDICTION"


def test_clean_case_false_positive_and_zero_break_edge():
    gt, row_ids = load_case(DATA, "case_01_clean_small")
    assert gt["breaks"] == []
    a = sorted(r for r in row_ids if r.startswith("A"))[:1]
    b = sorted(r for r in row_ids if r.startswith("B"))[:1]
    d = run(gt, row_ids, [pred("P1", "AMOUNT_MISMATCH", a, b)])
    assert d["tp"] == 0 and d["fp"] == 1 and d["fn"] == 0
    # zero predictions on zero breaks: perfect silence
    d2 = run(gt, row_ids, [])
    assert d2["tp"] == d2["fp"] == d2["fn"] == 0


def test_schema_invalid_output_scores_all_fn(sig):
    gt, row_ids = sig
    d = score_case(gt["case_id"], gt, row_ids, {"nonsense": True}, False, "schema violation: test")
    assert d["tp"] == 0 and d["fp"] == 0 and d["fn"] == 3
    assert d["schema_valid"] is False


def test_end_to_end_scoring_and_determinism(tmp_path, sig):
    """Full-directory scoring over all 14 cases; evaluator runs twice -> identical bytes."""
    gt, _ = sig
    outdir = tmp_path / "outputs"
    outdir.mkdir()
    manifest = json.loads((DATA / "manifest.json").read_text())
    for c in manifest["cases"]:
        cid = c["case_id"]
        if cid == SIG:
            g = gtb(gt, "COMPOUND")
            breaks = [pred("P1", "COMPOUND", g["a_row_ids"], g["b_row_ids"], g["component_causes"])]
        else:
            breaks = []
        (outdir / f"{cid}.json").write_text(json.dumps(output(cid, breaks)))
    r1 = score_outputs(outdir, DATA)
    r2 = score_outputs(outdir, DATA)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    t = r1["totals"]
    n_gt = sum(c["n_breaks"] for c in manifest["cases"])
    assert t["tp"] == 1 and t["fp"] == 0 and t["fn"] == n_gt - 1
    assert t["clean_case_false_positives"] == 0
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    write_json(r1, p1)
    write_json(r2, p2)
    assert p1.read_bytes() == p2.read_bytes()


def test_compare_output(sig, tmp_path):
    gt, _ = sig
    base = {"schema_version": "1.0", "system": {"name": "b", "model": "m"},
            "totals": {"precision": 0.5, "recall": 0.2, "f1": 0.2857,
                       "cause_accuracy_over_tp": 0.5, "evidence_validity_rate": 0.8,
                       "clean_case_false_positives": 3, "tp": 2, "fp": 2, "fn": 8,
                       "n_predictions": 4}}
    sol = {"schema_version": "1.0", "system": {"name": "s", "model": "m"},
           "totals": {"precision": 1.0, "recall": 0.9, "f1": 0.9474,
                      "cause_accuracy_over_tp": 0.9, "evidence_validity_rate": 1.0,
                      "clean_case_false_positives": 0, "tp": 9, "fp": 0, "fn": 1,
                      "n_predictions": 9}}
    c = compare(base, sol)
    assert c["primary_metric"] == "f1"
    assert c["primary_delta"] == 0.6617
    assert c["delta"]["clean_case_false_positives"] == -3


def test_mock_provider_deterministic_no_credentials():
    m = MockProvider({"case_13": '{"breaks": []}'})
    r1 = m.complete("sys", "reconcile case_13 please")
    r2 = m.complete("sys", "reconcile case_13 please")
    assert r1 == r2 == '{"breaks": []}'
    fallback1 = m.complete("sys", "unregistered prompt")
    fallback2 = m.complete("sys", "unregistered prompt")
    assert fallback1 == fallback2
    assert len(m.calls) == 4
