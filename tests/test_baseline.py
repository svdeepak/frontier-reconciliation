"""M4 baseline pipeline tests — MockProvider only, no credentials.

These verify PLUMBING (prompt -> parse -> file -> evaluator), not baseline
quality. Real control numbers come from a real provider run.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon import baseline  # noqa: E402
from recon.evaluate import score_outputs  # noqa: E402
from recon.providers import MockProvider, provider_from_env  # noqa: E402

DATA = ROOT / "data"
SIG = "case_13_signature_adversarial"


@pytest.fixture()
def mock_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def test_provider_from_env_defaults_to_mock(mock_env):
    assert isinstance(provider_from_env(), MockProvider)


def test_strip_fences():
    assert baseline.strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert baseline.strip_fences('{"a": 1}') == '{"a": 1}'


def test_baseline_writes_all_cases_and_meta(tmp_path, mock_env):
    out = tmp_path / "baseline"
    meta = baseline.run(DATA, out)
    manifest = json.loads((DATA / "manifest.json").read_text())
    for c in manifest["cases"]:
        assert (out / f"{c['case_id']}.json").exists()
    assert (out / "_meta.json").exists()
    assert meta["provider"] == "mock"
    assert len(meta["cases"]) == manifest["n_cases"]


def test_baseline_outputs_scoreable_and_unparseable_handled(tmp_path, monkeypatch):
    """A mock that answers case_13 correctly-ish and garbles case_01 still
    yields a full, honestly-scored evaluation run."""
    gt = json.loads((DATA / "cases" / SIG / "ground_truth.json").read_text())
    comp = next(b for b in gt["breaks"] if b["break_type"] == "COMPOUND")
    good = json.dumps({
        "schema_version": "1.0", "case_id": SIG,
        "system": {"name": "baseline-v1", "model": "mock", "provider": "mock"},
        "breaks": [{
            "break_id": "P-001", "break_type": "COMPOUND",
            "component_causes": comp["component_causes"],
            "a_row_ids": comp["a_row_ids"], "b_row_ids": comp["b_row_ids"],
            "evidence": "fee and fx differ", "suggested_action": "review",
            "confidence": "HIGH",
        }],
    })
    mock = MockProvider({SIG: good, "case_01_clean_small": "NOT JSON {{{"})
    monkeypatch.setattr(baseline, "provider_from_env", lambda: mock)
    out = tmp_path / "baseline"
    meta = baseline.run(DATA, out)
    assert [f["case_id"] for f in meta["parse_failures"]] == ["case_01_clean_small"]
    assert "raw_reply" in meta["parse_failures"][0]  # actual reply preserved in meta
    unparsed = json.loads((out / "case_01_clean_small.json").read_text())
    assert unparsed["breaks"] == []

    results = score_outputs(out, DATA, "baseline-v1")
    t = results["totals"]
    assert t["tp"] == 1  # the signature COMPOUND detection
    sig_case = next(c for c in results["per_case"] if c["case_id"] == SIG)
    assert sig_case["tp"] == 1 and sig_case["tp_correct_cause"] == 1
    assert sorted(sig_case["missed_break_ids"]) == ["GT-002", "GT-003"]
    clean = next(c for c in results["per_case"] if c["case_id"] == "case_01_clean_small")
    assert clean["schema_valid"] and clean["fp"] == 0  # empty breaks after parse failure


def test_baseline_run_deterministic_with_mock(tmp_path, mock_env):
    o1, o2 = tmp_path / "b1", tmp_path / "b2"
    baseline.run(DATA, o1)
    baseline.run(DATA, o2)
    for p1 in sorted(o1.glob("case_*.json")):
        p2 = o2 / p1.name
        assert p1.read_bytes() == p2.read_bytes()
