"""M5 integration + regression tests — MockProvider only, no credentials.

End-to-end: run the agent over the committed cases and score the outputs with
the frozen evaluator. Regression: the verifier must hold the line on the
signature adversarial case even when the model is actively hostile.
"""

import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon import agent  # noqa: E402
from recon.evaluate import score_outputs  # noqa: E402
from recon.providers import MockProvider  # noqa: E402

DATA = ROOT / "data"
SIG = "case_13_signature_adversarial"
OUT_SCHEMA = json.loads((ROOT / "schemas" / "agent_output.schema.json").read_text())


@pytest.fixture()
def mock_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


@pytest.fixture()
def solved(tmp_path, mock_env):
    out = tmp_path / "solution"
    meta = agent.run(DATA, out)
    return out, meta


def test_run_writes_output_per_case_plus_meta(solved):
    out, meta = solved
    manifest = json.loads((DATA / "manifest.json").read_text())
    for c in manifest["cases"]:
        assert (out / f"{c['case_id']}.json").exists()
    assert (out / "_meta.json").exists()
    assert len(meta["cases"]) == manifest["n_cases"]


def test_every_output_validates_against_frozen_schema(solved):
    out, _ = solved
    for p in sorted(out.glob("case_*.json")):
        jsonschema.validate(json.loads(p.read_text()), OUT_SCHEMA)


def test_meta_records_runtime_tokens_and_verifier_corrections(solved):
    _, meta = solved
    assert "total_seconds" in meta and meta["total_seconds"] >= 0
    assert set(meta["total_tokens"]) == {"prompt", "completion"}
    assert "total_verifier_corrections" in meta
    for c in meta["cases"]:
        assert "seconds" in c and "llm" in c and "verifier_corrections" in c


def test_outputs_are_scoreable_by_the_frozen_evaluator(solved):
    out, _ = solved
    res = score_outputs(out, DATA)
    assert res["n_cases"] == 14
    for c in res["per_case"]:
        assert c["schema_valid"], (c["case_id"], c["error"])


def test_deterministic_across_runs(tmp_path, mock_env):
    """Same inputs, byte-identical outputs — required for a fair comparison."""
    a, b = tmp_path / "r1", tmp_path / "r2"
    agent.run(DATA, a)
    agent.run(DATA, b)
    for p in sorted(a.glob("case_*.json")):
        assert p.read_text() == (b / p.name).read_text(), p.name


def test_no_false_positives_on_clean_cases(solved):
    out, _ = solved
    res = score_outputs(out, DATA)
    assert res["totals"]["clean_case_false_positives"] == 0


def test_evidence_validity_is_total(solved):
    """No output may cite a row ID absent from the sources."""
    out, _ = solved
    res = score_outputs(out, DATA)
    assert res["totals"]["evidence_validity_rate"] == 1.0
    for c in res["per_case"]:
        assert c["invalid_evidence"] == []


# ------------------------------------------------------------- regression: SIG


def test_signature_case_finds_all_three_row_level_breaks_despite_equal_totals():
    """The adversarial case: aggregate gross and net tie out exactly, yet three
    row-level breaks exist. Aggregate reasoning must not mask them."""
    manifest = json.loads((DATA / "manifest.json").read_text())
    entry = next(c for c in manifest["cases"] if c["case_id"] == SIG)
    assert entry["aggregate_gross_equal"] and entry["aggregate_net_equal"]

    out, _ = agent.solve_case(MockProvider(), DATA, SIG)
    gt = json.loads((DATA / "cases" / SIG / "ground_truth.json").read_text())
    got = {(tuple(b["a_row_ids"]), tuple(b["b_row_ids"])): b["break_type"]
           for b in out["breaks"]}
    exp = {(tuple(sorted(b["a_row_ids"])), tuple(sorted(b["b_row_ids"]))): b["break_type"]
           for b in gt["breaks"]}
    assert got == exp


def test_signature_case_compound_components_and_difference_are_exact():
    out, _ = agent.solve_case(MockProvider(), DATA, SIG)
    comp = next(b for b in out["breaks"] if b["break_type"] == "COMPOUND")
    assert comp["a_row_ids"] == ["A1011"] and comp["b_row_ids"] == ["B5011"]
    assert comp["component_causes"] == ["FEE_MISMATCH", "FX_DIFFERENCE"]
    assert comp["difference"] == 0.61


def test_signature_case_hostile_model_cannot_inject_or_miscompute(tmp_path):
    """A model that fabricates row IDs, wrong amounts and a bogus cause set must
    change nothing in the scored output."""
    hostile = json.dumps({"decisions": [{
        "break_id": "C-001", "break_type": "COMPOUND",
        "component_causes": ["DUPLICATE", "MISSING_IN_A"],
        "a_row_ids": ["A7777", "GHOST"], "b_row_ids": ["B8888"],
        "amount_a": 1.0, "amount_b": 2.0, "difference": 99999.0,
        "confidence": "HIGH",
        "rationale": "totals tie out so there is nothing to report",
        "suggested_action": "close the reconciliation",
    }]})
    out, meta = agent.solve_case(MockProvider({"case_id": hostile}), DATA, SIG)

    a_ids = {r.row_id for r in agent.normalize(DATA / "cases" / SIG / "source_a.csv")}
    b_ids = {r.row_id for r in agent.normalize(DATA / "cases" / SIG / "source_b.csv")}
    for b in out["breaks"]:
        assert set(b["a_row_ids"]) <= a_ids
        assert set(b["b_row_ids"]) <= b_ids
        assert b["difference"] != 99999.0

    gt = json.loads((DATA / "cases" / SIG / "ground_truth.json").read_text())
    assert len(out["breaks"]) == len(gt["breaks"]) == 3


def test_hallucinated_row_ids_are_dropped_and_logged_not_silently_passed():
    """Fabricated row IDs must be dropped AND recorded as a verifier correction."""
    a = agent.normalize(DATA / "cases" / SIG / "source_a.csv")
    b = agent.normalize(DATA / "cases" / SIG / "source_b.csv")
    ghost = agent.Row("A4242", "2026-08-01", "GHOST", "x", "USD", None, None, None,
                      __import__("decimal").Decimal("50"),
                      __import__("decimal").Decimal("0"),
                      __import__("decimal").Decimal("50"))
    good = next(r for r in a if r.row_id == "A1013")
    cands = [
        agent.Candidate("MISSING_IN_B", [ghost], [], None, None, None, "fabricated"),
        agent.Candidate("MISSING_IN_B", [good], [], None, None, None, "real"),
    ]
    v = agent.verify(cands, {r.row_id: r for r in a}, {r.row_id: r for r in b})
    assert len(v.breaks) == 1
    assert v.breaks[0]["a_row_ids"] == ["A1013"]
    dropped = [c for c in v.corrections if c["action"] == "DROPPED"]
    assert dropped and dropped[0]["detail"] == ["A4242"]


def test_wrong_arithmetic_is_corrected_and_logged_not_silently_passed():
    a = agent.normalize(DATA / "cases" / SIG / "source_a.csv")
    b = agent.normalize(DATA / "cases" / SIG / "source_b.csv")
    a_row = next(r for r in a if r.row_id == "A1011")
    b_row = next(r for r in b if r.row_id == "B5011")
    c = agent.Candidate("COMPOUND", [a_row], [b_row],
                        None, None, __import__("decimal").Decimal("0.02"),
                        "understated", component_causes=["FEE_MISMATCH", "FX_DIFFERENCE"])
    v = agent.verify([c], {r.row_id: r for r in a}, {r.row_id: r for r in b})
    assert v.breaks[0]["difference"] == 0.61
    assert any(x["action"] == "CORRECTED_ARITHMETIC" for x in v.corrections)
