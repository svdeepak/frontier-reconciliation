"""Break taxonomy — authoritative enum for the evaluation contract (v1.0).

JSON Schemas in /schemas embed the same values; tests/test_contract.py
asserts they never drift apart.
"""

from enum import Enum

SCHEMA_VERSION = "1.0"


class BreakType(str, Enum):
    MATCH = "MATCH"
    MISSING_IN_A = "MISSING_IN_A"
    MISSING_IN_B = "MISSING_IN_B"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    FEE_MISMATCH = "FEE_MISMATCH"
    FX_DIFFERENCE = "FX_DIFFERENCE"
    ROUNDING_DIFFERENCE = "ROUNDING_DIFFERENCE"
    DUPLICATE = "DUPLICATE"
    AMBIGUOUS = "AMBIGUOUS"
    COMPOUND = "COMPOUND"


# Types that count as breaks for detection scoring (everything except MATCH).
BREAK_TYPES = frozenset(t.value for t in BreakType if t is not BreakType.MATCH)

# Rounding tolerance in currency units per row (see EVALUATION.md §2).
ROUNDING_TOLERANCE = 0.01
