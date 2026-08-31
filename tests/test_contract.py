"""M1 contract tests.

Validates that (1) example fixtures conform to the JSON Schemas, (2) the
schema enums stay in sync with the authoritative taxonomy module, and
(3) invalid outputs are rejected.
"""

import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recon.taxonomy import BREAK_TYPES, BreakType, SCHEMA_VERSION  # noqa: E402

SCHEMAS = ROOT / "schemas"
FIXTURES = ROOT / "tests" / "fixtures"


def load(p: Path):
    return json.loads(p.read_text())


@pytest.fixture(scope="module")
def gt_schema():
    return load(SCHEMAS / "ground_truth.schema.json")


@pytest.fixture(scope="module")
def out_schema():
    return load(SCHEMAS / "agent_output.schema.json")


@pytest.fixture(scope="module")
def txn_schema():
    return load(SCHEMAS / "transaction.schema.json")


def test_ground_truth_fixture_validates(gt_schema):
    jsonschema.validate(load(FIXTURES / "ground_truth.example.json"), gt_schema)


def test_agent_output_fixture_validates(out_schema):
    jsonschema.validate(load(FIXTURES / "agent_output.example.json"), out_schema)


def test_transaction_fixture_validates(txn_schema):
    for row in load(FIXTURES / "transactions.example.json"):
        jsonschema.validate(row, txn_schema)


def test_schema_enums_match_taxonomy(gt_schema, out_schema):
    gt_enum = set(gt_schema["properties"]["breaks"]["items"]["properties"]["break_type"]["enum"])
    out_enum = set(out_schema["properties"]["breaks"]["items"]["properties"]["break_type"]["enum"])
    assert gt_enum == BREAK_TYPES
    assert out_enum == BREAK_TYPES
    assert BreakType.MATCH.value not in gt_enum  # MATCH is not a break


def test_schema_version_consistent(gt_schema, out_schema):
    assert gt_schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert out_schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION


def test_invalid_output_rejected(out_schema):
    bad = load(FIXTURES / "agent_output.example.json")
    bad["breaks"][0]["break_type"] = "MATCH"  # not a valid break type
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, out_schema)


def test_missing_evidence_rejected(out_schema):
    bad = load(FIXTURES / "agent_output.example.json")
    del bad["breaks"][0]["evidence"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, out_schema)
