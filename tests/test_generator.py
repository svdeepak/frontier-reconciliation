"""M2 generator tests: determinism, integrity, signature-case invariants."""

import csv
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.generate import MASTER_SEED, generate  # noqa: E402
from recon.taxonomy import BREAK_TYPES  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture(scope="module")
def manifest():
    if not (DATA / "manifest.json").exists():
        generate(MASTER_SEED, str(DATA))
    return json.loads((DATA / "manifest.json").read_text())


def tree_hash(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def load_case(case_id: str):
    d = DATA / "cases" / case_id
    gt = json.loads((d / "ground_truth.json").read_text())
    rows = {}
    for src in ("source_a.csv", "source_b.csv"):
        with open(d / src) as f:
            for r in csv.DictReader(f):
                rows[r["row_id"]] = r
    return gt, rows


def test_determinism_byte_identical(tmp_path, manifest):
    """Two fresh runs with the same seed produce byte-identical trees,
    which also match the committed datasets."""
    r1, r2 = tmp_path / "run1", tmp_path / "run2"
    generate(MASTER_SEED, str(r1))
    generate(MASTER_SEED, str(r2))
    h1, h2, hc = tree_hash(r1), tree_hash(r2), tree_hash(DATA)
    assert h1 == h2, "same seed produced different bytes across runs"
    assert h1 == hc, "committed datasets do not match regenerated output"


def test_case_count_and_composition(manifest):
    assert 12 <= manifest["n_cases"] <= 15
    seen = set()
    for c in manifest["cases"]:
        seen.update(c["breaks_by_type"])
    assert seen == BREAK_TYPES, f"taxonomy coverage gap: missing {BREAK_TYPES - seen}"


def test_clean_cases_have_no_breaks(manifest):
    clean = [c for c in manifest["cases"] if c["case_id"].startswith(("case_01", "case_02"))]
    assert len(clean) == 2
    assert all(c["n_breaks"] == 0 for c in clean)


def test_all_ground_truths_validate_and_reference_real_rows(manifest):
    schema = json.loads((ROOT / "schemas" / "ground_truth.schema.json").read_text())
    for c in manifest["cases"]:
        gt, rows = load_case(c["case_id"])
        jsonschema.validate(gt, schema)
        for m in gt["matches"]:
            assert m["a_row_id"] in rows and m["b_row_id"] in rows
        for br in gt["breaks"]:
            for rid in br["a_row_ids"] + br["b_row_ids"]:
                assert rid in rows, f"{c['case_id']} {br['break_id']} references missing row {rid}"
        # missing_in_* semantics: the absent side really is absent
        for br in gt["breaks"]:
            if br["break_type"] == "MISSING_IN_B":
                assert br["b_row_ids"] == []
            if br["break_type"] == "MISSING_IN_A":
                assert br["a_row_ids"] == []


def test_rows_internally_consistent(manifest):
    for c in manifest["cases"]:
        _, rows = load_case(c["case_id"])
        for r in rows.values():
            assert Decimal(r["net_amount"]) == Decimal(r["gross_amount"]) - Decimal(r["fee_amount"])


def test_signature_case_invariants(manifest):
    """The signature adversarial case: aggregates EQUAL, row-level breaks EXIST."""
    entry = next(c for c in manifest["cases"] if "signature" in c["case_id"])
    t = entry["totals"]
    assert t["a_gross"] == t["b_gross"], f"gross totals differ: {t}"
    assert t["a_net"] == t["b_net"], f"net totals differ: {t}"
    assert entry["n_breaks"] >= 3

    gt, rows = load_case(entry["case_id"])
    types = {b["break_type"] for b in gt["breaks"]}
    assert {"COMPOUND", "DUPLICATE", "MISSING_IN_B"} <= types

    comp = next(b for b in gt["breaks"] if b["break_type"] == "COMPOUND")
    assert set(comp["component_causes"]) == {"FEE_MISMATCH", "FX_DIFFERENCE"}
    # FX arithmetic is explicit: recompute both roundings from source fields
    a = rows[comp["a_row_ids"][0]]
    b = rows[comp["b_row_ids"][0]]
    from decimal import ROUND_DOWN, ROUND_HALF_UP
    raw = Decimal(a["foreign_amount"]) * Decimal(a["fx_rate"])
    assert Decimal(a["gross_amount"]) == raw.quantize(Decimal("0.01"), ROUND_HALF_UP)
    assert Decimal(b["gross_amount"]) == raw.quantize(Decimal("0.01"), ROUND_DOWN)
    assert Decimal(a["fee_amount"]) != Decimal(b["fee_amount"])
    assert Decimal(comp["expected_difference"] and str(comp["expected_difference"])) == Decimal("0.61")


def test_aggregate_trap_invariants(manifest):
    entry = next(c for c in manifest["cases"] if "aggregate_trap" in c["case_id"])
    t = entry["totals"]
    assert t["a_gross"] == t["b_gross"] and t["a_net"] == t["b_net"]
    assert entry["n_breaks"] == 2
    assert entry["breaks_by_type"] == {"AMOUNT_MISMATCH": 2}
