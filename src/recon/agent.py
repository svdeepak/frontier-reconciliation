"""Agent reconciliation pipeline (M5). This is the experimental TREATMENT.

Five stages, in order:

  1. normalize          — parse CSVs into typed rows; Decimal money, canonical
                          references, parsed dates. No inference.
  2. candidate matching — deterministic 1:1 pairing by reference / amount /
                          date heuristics, strongest signal first.
  3. deterministic classification — arithmetic over the paired and unpaired
                          rows produces candidate breaks with computed
                          differences.
  4. LLM reasoning      — interpretation ONLY, and only on rows the
                          deterministic stage could not settle (ambiguous
                          clusters, and cause selection where several causes
                          are arithmetically consistent). Never arithmetic.
  5. verification       — recompute every claimed difference from the source
                          rows; drop breaks citing nonexistent row IDs and
                          correct wrong arithmetic. Nothing reaches the output
                          file without passing this stage.

Contract boundary (docs/EVALUATION.md §5): every number in the output is
computed by code in this module. The LLM contributes labels and prose only;
its arithmetic is never trusted, and a hallucinated row ID cannot survive
stage 5.

Usage: python -m recon.agent [--data data] [--outputs outputs/solution]
Provider comes from environment (LLM_PROVIDER / see .env.example).
"""

import argparse
import csv
import json
import re
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from recon.providers import provider_from_env
from recon.taxonomy import ROUNDING_TOLERANCE, SCHEMA_VERSION

SYSTEM_NAME = "agent-v1"
CENT = Decimal("0.01")
TOLERANCE = Decimal(str(ROUNDING_TOLERANCE))
# Date window (days) within which two rows may still be the same transaction.
DATE_WINDOW = 3


def d2(x) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- stage 1


@dataclass(frozen=True)
class Row:
    """One normalized transaction row. Money is Decimal, never float."""

    row_id: str
    date: str
    reference: str
    description: str
    currency: str
    fx_rate: Decimal | None
    foreign_currency: str | None
    foreign_amount: Decimal | None
    gross_amount: Decimal
    fee_amount: Decimal
    net_amount: Decimal

    @property
    def ref_key(self) -> str:
        """Canonical reference: case/separator/prefix noise removed.

        'PB/INV-70055', 'inv 70055' and 'INV-70055' all collapse to 'INV70055'.
        Deliberately conservative — it strips known benign noise only, so two
        genuinely different references never collide.
        """
        r = self.reference.upper().strip()
        r = re.sub(r"^PB[/\-\s]+", "", r)
        return re.sub(r"[^A-Z0-9]", "", r)

    @property
    def day(self) -> int:
        y, m, d = (int(p) for p in self.date.split("-"))
        return y * 372 + m * 31 + d


def _dec(s: str) -> Decimal | None:
    s = (s or "").strip()
    return Decimal(s) if s else None


def normalize(csv_path: Path) -> list[Row]:
    rows: list[Row] = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            gross = _dec(r["gross_amount"]) or Decimal("0")
            fee = _dec(r["fee_amount"]) or Decimal("0")
            net = _dec(r.get("net_amount", "")) 
            rows.append(Row(
                row_id=r["row_id"].strip(),
                date=r["date"].strip(),
                reference=r.get("reference", "").strip(),
                description=r.get("description", "").strip(),
                currency=r.get("currency", "").strip(),
                fx_rate=_dec(r.get("fx_rate", "")),
                foreign_currency=(r.get("foreign_currency") or "").strip() or None,
                foreign_amount=_dec(r.get("foreign_amount", "")),
                gross_amount=gross,
                fee_amount=fee,
                net_amount=net if net is not None else gross - fee,
            ))
    return rows


# ---------------------------------------------------------------- stage 2


@dataclass
class MatchResult:
    pairs: list[tuple[Row, Row]] = field(default_factory=list)
    dup_groups_a: list[list[Row]] = field(default_factory=list)
    dup_groups_b: list[list[Row]] = field(default_factory=list)
    ambiguous_a: list[Row] = field(default_factory=list)
    ambiguous_b: list[Row] = field(default_factory=list)
    unmatched_a: list[Row] = field(default_factory=list)
    unmatched_b: list[Row] = field(default_factory=list)


def _dup_signature(r: Row) -> tuple:
    """Rows sharing this signature book the same economic transaction."""
    return (r.ref_key, r.currency, r.gross_amount, r.fee_amount)


def find_duplicates(rows: list[Row]) -> tuple[list[list[Row]], list[Row]]:
    """Group rows by economic signature. Deterministic, no LLM (§5).

    Returns (duplicate_groups, representatives) where each group has >= 2 rows
    and the representative is the group's first row by row_id — the one that
    remains eligible to match the other source.
    """
    by_sig: dict[tuple, list[Row]] = {}
    for r in rows:
        by_sig.setdefault(_dup_signature(r), []).append(r)
    groups, reps = [], []
    for _, grp in sorted(by_sig.items(), key=lambda kv: str(kv[0])):
        grp = sorted(grp, key=lambda r: r.row_id)
        if len(grp) > 1:
            groups.append(grp)
        reps.append(grp[0])
    return groups, reps


def _amount_key(r: Row) -> tuple:
    return (r.currency, r.gross_amount, r.fee_amount)


def match_candidates(a_rows: list[Row], b_rows: list[Row]) -> MatchResult:
    """Deterministic candidate matching, strongest signal first.

    Pass 1: unique canonical reference on both sides (survives casing,
            separator and prefix noise).
    Pass 2: exact (currency, gross, fee) where unique on both sides.
    Pass 3: same currency + near amount within a date window, greedily by
            smallest amount delta then smallest date gap — this is what
            surfaces amount/fee/FX discrepancies as pairs rather than as
            spurious missing-row breaks.

    Leftover same-day identical-amount clusters with unusable references are
    handed to stage 4 as ambiguous rather than force-paired.
    """
    res = MatchResult()
    res.dup_groups_a, a_pool = find_duplicates(a_rows)
    res.dup_groups_b, b_pool = find_duplicates(b_rows)

    a_left = {r.row_id: r for r in a_pool}
    b_left = {r.row_id: r for r in b_pool}

    def _pair(a: Row, b: Row):
        res.pairs.append((a, b))
        a_left.pop(a.row_id, None)
        b_left.pop(b.row_id, None)

    # Pass 1 — unique canonical reference.
    a_by_ref: dict[str, list[Row]] = {}
    b_by_ref: dict[str, list[Row]] = {}
    for r in a_left.values():
        a_by_ref.setdefault(r.ref_key, []).append(r)
    for r in b_left.values():
        b_by_ref.setdefault(r.ref_key, []).append(r)
    for ref in sorted(set(a_by_ref) & set(b_by_ref)):
        if len(a_by_ref[ref]) == 1 and len(b_by_ref[ref]) == 1 and _usable_ref(ref):
            _pair(a_by_ref[ref][0], b_by_ref[ref][0])

    # Pass 2 — exact amount identity, unique on both sides.
    a_by_amt: dict[tuple, list[Row]] = {}
    b_by_amt: dict[tuple, list[Row]] = {}
    for r in a_left.values():
        a_by_amt.setdefault(_amount_key(r), []).append(r)
    for r in b_left.values():
        b_by_amt.setdefault(_amount_key(r), []).append(r)
    for key in sorted(set(a_by_amt) & set(b_by_amt), key=str):
        if len(a_by_amt[key]) == 1 and len(b_by_amt[key]) == 1:
            _pair(a_by_amt[key][0], b_by_amt[key][0])

    # Ambiguity check runs BEFORE pass 3: an unresolvable cluster must not be
    # force-paired by amount+date proximity just because the numbers align.
    amb_a, amb_b = _ambiguous_clusters(list(a_left.values()), list(b_left.values()))
    for r in amb_a:
        a_left.pop(r.row_id, None)
    for r in amb_b:
        b_left.pop(r.row_id, None)

    # Pass 3 — near amount within a date window, greedy by best delta.
    cands = []
    for a in a_left.values():
        for b in b_left.values():
            if a.currency != b.currency:
                continue
            gap = abs(a.day - b.day)
            if gap > DATE_WINDOW:
                continue
            delta = abs(a.net_amount - b.net_amount)
            rel = delta / a.gross_amount if a.gross_amount else delta
            if rel > Decimal("0.05"):
                continue
            cands.append((delta, gap, a.row_id, b.row_id, a, b))
    for _delta, _gap, aid, bid, a, b in sorted(cands, key=lambda t: (t[0], t[1], t[2], t[3])):
        if aid in a_left and bid in b_left:
            _pair(a, b)

    # Whatever remains after pass 3 is genuinely unmatched.
    res.ambiguous_a, res.ambiguous_b = amb_a, amb_b
    res.unmatched_a = sorted(a_left.values(), key=lambda r: r.row_id)
    res.unmatched_b = sorted(b_left.values(), key=lambda r: r.row_id)
    res.pairs.sort(key=lambda p: (p[0].row_id, p[1].row_id))
    return res


UNUSABLE_REF = re.compile(r"^(REF)?(ILLEGIBLE|MISSING|UNKNOWN|NA)?$")


def _usable_ref(ref_key: str) -> bool:
    """A reference carries identity unless it is empty or a placeholder."""
    if not ref_key:
        return False
    return not UNUSABLE_REF.match(ref_key)


def _ambiguous_clusters(a_left: list[Row], b_left: list[Row]) -> tuple[list[Row], list[Row]]:
    """Same-currency, same-amount groups with >= 2 rows on BOTH sides and no
    usable reference to discriminate — correspondence is not establishable.
    """
    amb_a: list[Row] = []
    amb_b: list[Row] = []
    keys = {_amount_key(r) for r in a_left} & {_amount_key(r) for r in b_left}
    for key in sorted(keys, key=str):
        ga = [r for r in a_left if _amount_key(r) == key]
        gb = [r for r in b_left if _amount_key(r) == key]
        if len(ga) < 2 or len(gb) < 2:
            continue
        # Correspondence is establishable only if BOTH sides carry usable
        # references for every row in the cluster; if either side is blind,
        # equal amounts and dates give no basis to pick a pairing.
        if all(_usable_ref(r.ref_key) for r in ga) and all(_usable_ref(r.ref_key) for r in gb):
            continue
        amb_a.extend(sorted(ga, key=lambda r: r.row_id))
        amb_b.extend(sorted(gb, key=lambda r: r.row_id))
    return amb_a, amb_b
