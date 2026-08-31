"""M5 agent pipeline tests — MockProvider only, no credentials.

Unit coverage of the deterministic stages (normalization, matching, cause
classification, verifier arithmetic) plus the LLM boundary contract: the
model may relabel and annotate, never compute.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon import agent  # noqa: E402
from recon.providers import MockProvider  # noqa: E402

DATA = ROOT / "data"
SIG = "case_13_signature_adversarial"


def load(cid):
    d = DATA / "cases" / cid
    return agent.normalize(d / "source_a.csv"), agent.normalize(d / "source_b.csv")


def gt(cid):
    return json.loads((DATA / "cases" / cid / "ground_truth.json").read_text())


# ---------------------------------------------------------------- normalize


def test_normalize_uses_decimal_money_not_float():
    a, _ = load(SIG)
    r = {x.row_id: x for x in a}["A1011"]
    assert isinstance(r.gross_amount, Decimal)
    assert r.gross_amount == Decimal("1340.30")
    assert r.net_amount == Decimal("1337.80")
    assert r.foreign_amount == Decimal("1234.50")
    assert r.fx_rate == Decimal("1.0857")


def test_normalize_derives_net_when_absent(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("row_id,date,reference,description,currency,fx_rate,foreign_currency,"
                 "foreign_amount,gross_amount,fee_amount,net_amount\n"
                 "A1001,2026-08-01,INV-1,x,USD,,,,100.00,1.50,\n")
    assert agent.normalize(p)[0].net_amount == Decimal("98.50")


@pytest.mark.parametrize("raw,expected", [
    ("INV-70055", "INV70055"),
    ("inv 70055", "INV70055"),
    ("PB/INV-70055", "INV70055"),
    ("PB INV-70055", "INV70055"),
])
def test_reference_noise_collapses_to_same_key(raw, expected, tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("row_id,date,reference,description,currency,fx_rate,foreign_currency,"
                 "foreign_amount,gross_amount,fee_amount,net_amount\n"
                 f"A1001,2026-08-01,{raw},x,USD,,,,100.00,1.00,99.00\n")
    assert agent.normalize(p)[0].ref_key == expected


def test_distinct_references_do_not_collide():
    a, _ = load("case_11_ambiguous")
    keys = {r.row_id: r.ref_key for r in a}
    assert keys["A1009"] != keys["A1010"]


# ---------------------------------------------------------------- matching


def test_clean_cases_pair_completely_and_yield_no_breaks():
    for cid in ("case_01_clean_small", "case_02_clean_messy"):
        a, b = load(cid)
        m = agent.match_candidates(a, b)
        assert len(m.pairs) == len(a) == len(b), cid
        assert agent.build_candidates(m) == [], cid


def test_duplicates_grouped_by_economic_signature():
    a, b = load("case_10_duplicates")
    m = agent.match_candidates(a, b)
    groups = sorted(sorted(r.row_id for r in g) for g in m.dup_groups_b)
    assert groups == [["B5011", "B5011D"], ["B5012", "B5012D"]]
    assert m.dup_groups_a == []


def test_ambiguous_cluster_is_not_force_paired():
    """Equal amounts and dates must not manufacture a pairing when one side's
    references are unusable — the cluster stays ambiguous."""
    a, b = load("case_11_ambiguous")
    m = agent.match_candidates(a, b)
    assert sorted(r.row_id for r in m.ambiguous_a) == ["A1009", "A1010"]
    assert sorted(r.row_id for r in m.ambiguous_b) == ["B5009", "B5010"]
    paired = {r.row_id for p in m.pairs for r in p}
    assert not paired & {"A1009", "A1010", "B5009", "B5010"}


def test_unmatched_rows_are_only_the_genuinely_missing_ones():
    a, b = load("case_03_missing_in_b")
    m = agent.match_candidates(a, b)
    assert [r.row_id for r in m.unmatched_a] == ["A1011", "A1012"]
    assert m.unmatched_b == []


def test_discrepant_pairs_stay_paired_not_reported_missing():
    """An amount mismatch must surface as one break, not two missing rows."""
    a, b = load("case_05_amount_mismatch")
    m = agent.match_candidates(a, b)
    assert m.unmatched_a == [] and m.unmatched_b == []
    assert len(m.pairs) == len(a)


# ------------------------------------------------------- cause classification


def _pair(gross_a, fee_a, gross_b, fee_b, **kw):
    def mk(rid, gross, fee):
        return agent.Row(rid, "2026-08-01", "INV-1", "x", "USD",
                         kw.get("fx_rate"), kw.get("fcur"), kw.get("famt"),
                         Decimal(gross), Decimal(fee), Decimal(gross) - Decimal(fee))
    return mk("A1001", gross_a, fee_a), mk("B5001", gross_b, fee_b)


def test_identical_pair_is_not_a_break():
    a, b = _pair("100.00", "1.00", "100.00", "1.00")
    assert agent.classify_pair(a, b) is None


def test_amount_mismatch_beyond_tolerance():
    c = agent.classify_pair(*_pair("100.00", "1.00", "88.00", "1.00"))
    assert c.break_type == "AMOUNT_MISMATCH"
    assert c.difference == Decimal("12.00")


def test_one_cent_difference_is_rounding_not_amount_mismatch():
    c = agent.classify_pair(*_pair("100.00", "1.00", "99.99", "1.00"))
    assert c.break_type == "ROUNDING_DIFFERENCE"


def test_fee_only_difference_reports_fee_amounts():
    c = agent.classify_pair(*_pair("100.00", "1.00", "100.00", "1.60"))
    assert c.break_type == "FEE_MISMATCH"
    assert (c.amount_a, c.amount_b, c.difference) == (
        Decimal("1.00"), Decimal("1.60"), Decimal("-0.60"))


def test_fx_attribution_requires_same_foreign_amount_and_rate():
    """A one-cent gross gap on identical foreign amount+rate is FX, not rounding."""
    c = agent.classify_pair(*_pair("1340.30", "2.50", "1340.29", "2.50",
                                   fx_rate=Decimal("1.0857"), fcur="EUR",
                                   famt=Decimal("1234.50")))
    assert c.break_type == "FX_DIFFERENCE"


def test_two_causes_become_compound_with_full_component_set():
    c = agent.classify_pair(*_pair("1340.30", "2.50", "1340.29", "3.10",
                                   fx_rate=Decimal("1.0857"), fcur="EUR",
                                   famt=Decimal("1234.50")))
    assert c.break_type == "COMPOUND"
    assert c.component_causes == ["FEE_MISMATCH", "FX_DIFFERENCE"]
    assert c.difference == Decimal("0.61")


# ---------------------------------------------------------------- verifier


def _verify_one(c, a_rows, b_rows):
    return agent.verify([c], {r.row_id: r for r in a_rows}, {r.row_id: r for r in b_rows})


def test_verifier_drops_break_citing_nonexistent_row_id():
    a, b = load(SIG)
    c = agent.Candidate("AMOUNT_MISMATCH", [], [], None, None, None, "fabricated")
    c.a_rows = [agent.Row("A9999", "2026-08-01", "X", "x", "USD", None, None, None,
                          Decimal("1"), Decimal("0"), Decimal("1"))]
    v = _verify_one(c, a, b)
    assert v.breaks == []
    assert v.corrections[0]["reason"] == "nonexistent_row_ids"
    assert v.corrections[0]["detail"] == ["A9999"]


def test_verifier_overwrites_wrong_arithmetic_with_recomputed_values():
    a, b = load(SIG)
    a_row = {r.row_id: r for r in a}["A1011"]
    b_row = {r.row_id: r for r in b}["B5011"]
    c = agent.Candidate("FEE_MISMATCH", [a_row], [b_row],
                        Decimal("999.00"), Decimal("111.00"), Decimal("888.00"),
                        "claimed nonsense")
    v = _verify_one(c, a, b)
    assert v.breaks[0]["amount_a"] == 2.50
    assert v.breaks[0]["amount_b"] == 3.10
    assert v.breaks[0]["difference"] == -0.60
    reasons = {x["reason"] for x in v.corrections}
    assert reasons == {"amount_a", "amount_b", "difference"}
    assert all(x["action"] == "CORRECTED_ARITHMETIC" for x in v.corrections)


def test_verifier_drops_compound_with_unsupported_component_set():
    """A COMPOUND claim whose components arithmetic does not support is dropped."""
    a, b = load(SIG)
    a_row = {r.row_id: r for r in a}["A1011"]
    b_row = {r.row_id: r for r in b}["B5011"]
    c = agent.Candidate("COMPOUND", [a_row], [b_row], None, None, None, "e",
                        component_causes=["DUPLICATE", "MISSING_IN_A"])
    v = _verify_one(c, a, b)
    assert v.breaks == []
    assert v.corrections[0]["reason"] == "compound_components_not_arithmetically_supported"


def test_verifier_drops_break_type_outside_taxonomy():
    a, b = load(SIG)
    a_row = {r.row_id: r for r in a}["A1013"]
    c = agent.Candidate("TOTALLY_MADE_UP", [a_row], [], None, None, None, "e")
    v = _verify_one(c, a, b)
    assert v.breaks == []
    assert v.corrections[0]["reason"] == "break_type_not_in_taxonomy"


def test_verifier_emits_valid_schema_fields_and_sequential_ids():
    a, b = load(SIG)
    m = agent.match_candidates(a, b)
    v = agent.verify(agent.build_candidates(m),
                     {r.row_id: r for r in a}, {r.row_id: r for r in b})
    assert [x["break_id"] for x in v.breaks] == ["P-001", "P-002", "P-003"]
    for x in v.breaks:
        assert x["confidence"] in ("HIGH", "MEDIUM", "LOW")
        assert x["evidence"] and x["suggested_action"]


# ------------------------------------------------------------- LLM boundary


def test_no_llm_call_when_nothing_is_ambiguous():
    p = MockProvider()
    out, meta = agent.solve_case(p, DATA, "case_05_amount_mismatch")
    assert meta["llm"]["called"] is False
    assert p.calls == []


def test_llm_is_called_only_for_ambiguous_candidates():
    p = MockProvider()
    out, meta = agent.solve_case(p, DATA, "case_11_ambiguous")
    assert meta["llm"]["called"] is True
    assert meta["llm"]["n_candidates"] == 1
    assert len(p.calls) == 1


def test_llm_prompt_forbids_arithmetic_and_carries_computed_values():
    p = MockProvider()
    agent.solve_case(p, DATA, "case_11_ambiguous")
    system, user = p.calls[0]
    assert "NEVER perform arithmetic" in system
    assert "Do not invent row IDs" in system
    assert "computed_difference" in user


def test_malformed_llm_reply_degrades_to_deterministic_result():
    p = MockProvider({"case_id": "not json at all {{{"})
    out, meta = agent.solve_case(p, DATA, "case_11_ambiguous")
    assert meta["llm"]["error"] is not None
    assert [b["break_type"] for b in out["breaks"]] == ["AMBIGUOUS"]


def test_llm_cannot_alter_computed_amounts():
    """Even when the model returns amounts, output numbers come from source rows."""
    gtb = gt("case_11_ambiguous")["breaks"][0]
    reply = json.dumps({"decisions": [{
        "break_id": "C-001", "break_type": "AMBIGUOUS", "confidence": "LOW",
        "amount_a": 999999.99, "difference": -12345.0,
        "rationale": "References unusable on the partner side.",
        "suggested_action": "Request full remittance detail.",
    }]})
    p = MockProvider({"case_id": reply})
    out, _ = agent.solve_case(p, DATA, "case_11_ambiguous")
    br = out["breaks"][0]
    assert br["amount_a"] != 999999.99
    assert br["difference"] == 0.0
    assert set(br["a_row_ids"]) == set(gtb["a_row_ids"])
    assert "References unusable" in br["evidence"]
    assert br["suggested_action"] == "Request full remittance detail."


def test_llm_cannot_introduce_hallucinated_row_ids():
    """Row sets are structural: the model has no channel to add row IDs."""
    reply = json.dumps({"decisions": [{
        "break_id": "C-001", "break_type": "AMBIGUOUS", "confidence": "LOW",
        "a_row_ids": ["A0000", "GHOST1"], "b_row_ids": ["B9999"],
        "rationale": "x", "suggested_action": "y",
    }]})
    p = MockProvider({"case_id": reply})
    out, _ = agent.solve_case(p, DATA, "case_11_ambiguous")
    cited = set(out["breaks"][0]["a_row_ids"]) | set(out["breaks"][0]["b_row_ids"])
    assert not cited & {"A0000", "GHOST1", "B9999"}


def test_llm_relabel_outside_taxonomy_is_rejected():
    reply = json.dumps({"decisions": [{
        "break_id": "C-001", "break_type": "SOMETHING_INVENTED",
        "confidence": "HIGH", "rationale": "x", "suggested_action": "y",
    }]})
    p = MockProvider({"case_id": reply})
    out, meta = agent.solve_case(p, DATA, "case_11_ambiguous")
    assert out["breaks"][0]["break_type"] == "AMBIGUOUS"
    assert meta["llm_decisions_applied"][0]["accepted_type"] is False


def test_provider_failure_does_not_abort_the_run():
    class Boom(MockProvider):
        def complete(self, system, user):
            raise RuntimeError("transport exploded")

    out, meta = agent.solve_case(Boom(), DATA, "case_11_ambiguous")
    assert "transport exploded" in meta["llm"]["error"]
    assert len(out["breaks"]) == 1
