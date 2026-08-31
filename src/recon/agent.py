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


# ---------------------------------------------------------------- stage 3


@dataclass
class Candidate:
    """A break under construction. Every amount here is computed by code."""

    break_type: str
    a_rows: list[Row]
    b_rows: list[Row]
    amount_a: Decimal | None
    amount_b: Decimal | None
    difference: Decimal | None
    evidence: str
    component_causes: list[str] = field(default_factory=list)
    confidence: str = "HIGH"
    needs_llm: bool = False
    llm_note: str = ""

    @property
    def a_ids(self) -> list[str]:
        return sorted(r.row_id for r in self.a_rows)

    @property
    def b_ids(self) -> list[str]:
        return sorted(r.row_id for r in self.b_rows)


def _fx_explains(a: Row, b: Row) -> bool:
    """True iff both rows book the same foreign amount at the same rate, so a
    gross divergence is attributable to rounding-mode choice (contract §2).
    """
    if None in (a.fx_rate, b.fx_rate, a.foreign_amount, b.foreign_amount):
        return False
    return (a.fx_rate == b.fx_rate
            and a.foreign_amount == b.foreign_amount
            and a.foreign_currency == b.foreign_currency)


def classify_pair(a: Row, b: Row) -> Candidate | None:
    """Deterministic cause analysis for one matched pair (§5: no LLM here).

    Returns None when the pair reconciles exactly. Causes are established by
    arithmetic on gross and fee; when two or more apply the result is COMPOUND
    with the full component set.
    """
    gross_delta = a.gross_amount - b.gross_amount
    fee_delta = a.fee_amount - b.fee_amount
    causes: list[str] = []

    if gross_delta != 0:
        if _fx_explains(a, b):
            causes.append("FX_DIFFERENCE")
        elif abs(gross_delta) <= TOLERANCE:
            causes.append("ROUNDING_DIFFERENCE")
        else:
            causes.append("AMOUNT_MISMATCH")
    if fee_delta != 0:
        causes.append("FEE_MISMATCH")

    if not causes:
        return None

    net_delta = a.net_amount - b.net_amount
    bits = []
    if gross_delta != 0:
        bits.append(f"gross A {a.gross_amount} vs B {b.gross_amount} (delta {gross_delta})")
    if fee_delta != 0:
        bits.append(f"fee A {a.fee_amount} vs B {b.fee_amount} (delta {fee_delta})")
    if "FX_DIFFERENCE" in causes:
        bits.append(f"same {a.foreign_currency} {a.foreign_amount} @ {a.fx_rate}: "
                    f"A {a.gross_amount} vs B {b.gross_amount} — rounding-mode divergence")
    evidence = (f"Rows {a.row_id}/{b.row_id}: " + "; ".join(bits)
                + f". Net A {a.net_amount} vs B {b.net_amount} (delta {net_delta}).")

    if len(causes) > 1:
        return Candidate("COMPOUND", [a], [b], a.net_amount, b.net_amount, net_delta,
                         evidence, component_causes=sorted(causes))
    cause = causes[0]
    if cause == "FEE_MISMATCH":
        amt_a, amt_b, diff = a.fee_amount, b.fee_amount, fee_delta
    else:
        amt_a, amt_b, diff = a.gross_amount, b.gross_amount, gross_delta
    return Candidate(cause, [a], [b], amt_a, amt_b, diff, evidence)


def build_candidates(m: MatchResult) -> list[Candidate]:
    """All candidate breaks, deterministically, from a match partition.

    Row sets follow the contract's scoring convention (§4):
    duplicates cite only the duplicated source's rows; missing rows cite only
    the source that has them; ambiguous clusters are one break over all rows.
    """
    out: list[Candidate] = []

    for a, b in m.pairs:
        c = classify_pair(a, b)
        if c is not None:
            out.append(c)

    for grp in m.dup_groups_b:
        rep = grp[0]
        out.append(Candidate(
            "DUPLICATE", [], list(grp), None, rep.net_amount, rep.net_amount,
            f"B rows {'/'.join(r.row_id for r in grp)} share reference "
            f"{rep.reference!r}, currency {rep.currency}, gross {rep.gross_amount} and "
            f"fee {rep.fee_amount}: the same economic transaction booked "
            f"{len(grp)} times, inflating B by net {rep.net_amount}."))
    for grp in m.dup_groups_a:
        rep = grp[0]
        out.append(Candidate(
            "DUPLICATE", list(grp), [], rep.net_amount, None, rep.net_amount,
            f"A rows {'/'.join(r.row_id for r in grp)} share reference "
            f"{rep.reference!r}, currency {rep.currency}, gross {rep.gross_amount} and "
            f"fee {rep.fee_amount}: the same economic transaction booked "
            f"{len(grp)} times, inflating A by net {rep.net_amount}."))

    for r in m.unmatched_a:
        out.append(Candidate(
            "MISSING_IN_B", [r], [], r.net_amount, None, r.net_amount,
            f"A row {r.row_id} (ref {r.reference!r}, {r.currency} gross "
            f"{r.gross_amount}, net {r.net_amount}, dated {r.date}) has no "
            f"counterpart in B after reference, amount and date matching."))
    for r in m.unmatched_b:
        out.append(Candidate(
            "MISSING_IN_A", [], [r], None, r.net_amount, r.net_amount,
            f"B row {r.row_id} (ref {r.reference!r}, {r.currency} gross "
            f"{r.gross_amount}, net {r.net_amount}, dated {r.date}) has no "
            f"counterpart in A after reference, amount and date matching."))

    if m.ambiguous_a or m.ambiguous_b:
        aa, bb = m.ambiguous_a, m.ambiguous_b
        out.append(Candidate(
            "AMBIGUOUS", list(aa), list(bb),
            sum((r.net_amount for r in aa), Decimal("0")),
            sum((r.net_amount for r in bb), Decimal("0")),
            Decimal("0"),
            f"A rows {'/'.join(r.row_id for r in aa)} and B rows "
            f"{'/'.join(r.row_id for r in bb)} share currency and amount on "
            f"overlapping dates with unusable references "
            f"({', '.join(sorted({r.reference for r in bb + aa if not _usable_ref(r.ref_key)}))}); "
            f"correspondence cannot be established from the data.",
            confidence="LOW", needs_llm=True))

    out.sort(key=lambda c: (c.a_ids, c.b_ids, c.break_type))
    return out


# ---------------------------------------------------------------- stage 4

LLM_SYSTEM_PROMPT = """You are a payments reconciliation analyst. You classify and \
explain reconciliation exceptions. You NEVER perform arithmetic: every amount and \
difference has already been computed from the source rows and is authoritative.

You will receive candidate exceptions that deterministic matching could not settle, \
each with its computed amounts and the source rows involved.

For each candidate, choose the most defensible interpretation and return JSON only:
{"decisions": [{"break_id": "<the id given>", "break_type": "<one of the allowed types>",
  "component_causes": ["..."], "confidence": "HIGH"|"MEDIUM"|"LOW",
  "rationale": "<one sentence, business-meaningful>",
  "suggested_action": "<concrete next step for an operations analyst>"}]}

Allowed break_type values: MISSING_IN_A, MISSING_IN_B, AMOUNT_MISMATCH, FEE_MISMATCH, \
FX_DIFFERENCE, ROUNDING_DIFFERENCE, DUPLICATE, AMBIGUOUS, COMPOUND. Use \
component_causes only for COMPOUND. Cite ONLY row IDs that appear in the candidate. \
Do not invent row IDs. Do not restate or recompute the numbers."""

ALLOWED_TYPES = frozenset([
    "MISSING_IN_A", "MISSING_IN_B", "AMOUNT_MISMATCH", "FEE_MISMATCH", "FX_DIFFERENCE",
    "ROUNDING_DIFFERENCE", "DUPLICATE", "AMBIGUOUS", "COMPOUND",
])
CONFIDENCES = frozenset(["HIGH", "MEDIUM", "LOW"])


def _candidate_brief(idx: int, c: Candidate, a_by_id: dict, b_by_id: dict) -> dict:
    def describe(rid, src):
        r = (a_by_id if src == "A" else b_by_id)[rid]
        return {"row_id": r.row_id, "date": r.date, "reference": r.reference,
                "currency": r.currency, "gross_amount": str(r.gross_amount),
                "fee_amount": str(r.fee_amount), "net_amount": str(r.net_amount)}
    return {
        "break_id": f"C-{idx:03d}",
        "deterministic_break_type": c.break_type,
        "computed_amount_a": None if c.amount_a is None else str(c.amount_a),
        "computed_amount_b": None if c.amount_b is None else str(c.amount_b),
        "computed_difference": None if c.difference is None else str(c.difference),
        "a_rows": [describe(r, "A") for r in c.a_ids],
        "b_rows": [describe(r, "B") for r in c.b_ids],
        "computed_evidence": c.evidence,
    }


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def llm_interpret(provider, case_id: str, candidates: list[Candidate],
                  a_by_id: dict, b_by_id: dict) -> tuple[dict, dict]:
    """Ask the LLM to interpret ONLY the unsettled candidates (§5 boundary).

    Returns (decisions_by_break_id, call_meta). Returns empty decisions when
    there is nothing ambiguous — a fully deterministic case makes no LLM call
    at all. Malformed replies degrade to the deterministic result rather than
    failing the run; nothing here is trusted for arithmetic.
    """
    idx_map = {f"C-{i:03d}": c for i, c in enumerate(candidates, 1) if c.needs_llm}
    meta = {"called": False, "n_candidates": len(idx_map), "seconds": 0.0,
            "usage": {}, "error": None}
    if not idx_map:
        return {}, meta

    briefs = [_candidate_brief(i, c, a_by_id, b_by_id)
              for i, c in enumerate(candidates, 1) if c.needs_llm]
    user = (f"case_id: {case_id}\n\nCandidate exceptions requiring interpretation:\n"
            + json.dumps(briefs, indent=2))
    t0 = time.time()
    meta["called"] = True
    try:
        reply = provider.complete(LLM_SYSTEM_PROMPT, user)
        parsed = json.loads(strip_fences(reply))
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


def apply_decisions(candidates: list[Candidate], decisions: dict) -> list[dict]:
    """Merge LLM labels into candidates. Arithmetic fields are never touched.

    Each accepted or rejected label is recorded so the audit trail shows what
    the model was allowed to change.
    """
    applied: list[dict] = []
    for i, c in enumerate(candidates, 1):
        d = decisions.get(f"C-{i:03d}")
        if not d:
            continue
        note = {"break_id": f"C-{i:03d}", "deterministic_type": c.break_type,
                "llm_type": d.get("break_type"), "accepted_type": False,
                "accepted_confidence": False}
        t = d.get("break_type")
        if t in ALLOWED_TYPES:
            # The LLM may refine a cause only where code left it open.
            if c.break_type == "AMBIGUOUS":
                c.break_type = t
                if t == "COMPOUND":
                    comps = [x for x in d.get("component_causes", []) if x in ALLOWED_TYPES]
                    c.component_causes = sorted(set(comps))
                note["accepted_type"] = True
        conf = d.get("confidence")
        if conf in CONFIDENCES:
            c.confidence = conf
            note["accepted_confidence"] = True
        if isinstance(d.get("rationale"), str) and d["rationale"].strip():
            c.llm_note = d["rationale"].strip()
        if isinstance(d.get("suggested_action"), str) and d["suggested_action"].strip():
            c.suggested_action_override = d["suggested_action"].strip()
        applied.append(note)
    return applied


# ---------------------------------------------------------------- stage 5

SUGGESTED_ACTIONS = {
    "MISSING_IN_A": "Investigate why the partner booked this transaction with no ledger entry; post to the ledger or raise with the partner.",
    "MISSING_IN_B": "Chase the partner for the missing settlement line; withhold reconciliation sign-off until it appears or is written off.",
    "AMOUNT_MISMATCH": "Confirm the correct gross with the partner and post an adjustment for the difference.",
    "FEE_MISMATCH": "Verify the contracted fee schedule and recover or accrue the fee difference.",
    "FX_DIFFERENCE": "Align rounding convention on converted amounts with the partner; book the sub-cent difference to FX variance.",
    "ROUNDING_DIFFERENCE": "Accept within tolerance and book to the rounding variance account.",
    "DUPLICATE": "Void the duplicate booking and reverse its effect on the affected source's totals.",
    "AMBIGUOUS": "Obtain the partner's full reference detail to establish correspondence before clearing.",
    "COMPOUND": "Resolve each component cause separately; the net difference reflects more than one issue.",
}


@dataclass
class Verification:
    breaks: list[dict] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)

    @property
    def n_corrections(self) -> int:
        return len(self.corrections)


def verify(candidates: list[Candidate], a_by_id: dict, b_by_id: dict) -> Verification:
    """Deterministic verification pass — the output gate.

    For every candidate:
      * drop it if it cites a row ID absent from the source data;
      * recompute amount_a / amount_b / difference from the source rows and
        overwrite any value that disagrees;
      * drop COMPOUND claims whose component set is not arithmetically
        supported, and require a component set of at least two causes;
      * reject break types outside the frozen taxonomy.

    Every intervention is recorded in `corrections` so the run is auditable.
    """
    v = Verification()
    for i, c in enumerate(candidates, 1):
        cid = f"C-{i:03d}"

        missing = ([r for r in c.a_ids if r not in a_by_id]
                   + [r for r in c.b_ids if r not in b_by_id])
        if missing:
            v.corrections.append({"break_id": cid, "action": "DROPPED",
                                  "reason": "nonexistent_row_ids",
                                  "detail": sorted(missing)})
            continue

        if c.break_type not in ALLOWED_TYPES:
            v.corrections.append({"break_id": cid, "action": "DROPPED",
                                  "reason": "break_type_not_in_taxonomy",
                                  "detail": c.break_type})
            continue

        a_rows = [a_by_id[r] for r in c.a_ids]
        b_rows = [b_by_id[r] for r in c.b_ids]

        if c.break_type == "COMPOUND":
            comps = sorted(set(c.component_causes))
            if len(comps) < 2 or not set(comps) <= ALLOWED_TYPES:
                v.corrections.append({"break_id": cid, "action": "DROPPED",
                                      "reason": "compound_component_set_unsupported",
                                      "detail": comps})
                continue
            if len(a_rows) == 1 and len(b_rows) == 1:
                supported = _supported_causes(a_rows[0], b_rows[0])
                if set(comps) != supported:
                    v.corrections.append({
                        "break_id": cid, "action": "DROPPED",
                        "reason": "compound_components_not_arithmetically_supported",
                        "detail": {"claimed": comps, "supported": sorted(supported)}})
                    continue

        exp_a, exp_b, exp_diff = _recompute(c.break_type, a_rows, b_rows)
        for field_name, claimed, expected in (("amount_a", c.amount_a, exp_a),
                                              ("amount_b", c.amount_b, exp_b),
                                              ("difference", c.difference, exp_diff)):
            if claimed is None and expected is None:
                continue
            if claimed is None or expected is None or d2(claimed) != d2(expected):
                v.corrections.append({
                    "break_id": cid, "action": "CORRECTED_ARITHMETIC",
                    "reason": field_name,
                    "detail": {"claimed": None if claimed is None else str(claimed),
                               "recomputed": None if expected is None else str(expected)}})
        c.amount_a, c.amount_b, c.difference = exp_a, exp_b, exp_diff

        action = getattr(c, "suggested_action_override", None) or SUGGESTED_ACTIONS[c.break_type]
        evidence = c.evidence
        if c.llm_note:
            evidence = f"{evidence} Analyst interpretation: {c.llm_note}"

        b = {
            "break_id": f"P-{len(v.breaks) + 1:03d}",
            "break_type": c.break_type,
            "a_row_ids": c.a_ids,
            "b_row_ids": c.b_ids,
            "amount_a": None if exp_a is None else float(exp_a),
            "amount_b": None if exp_b is None else float(exp_b),
            "difference": None if exp_diff is None else float(exp_diff),
            "evidence": evidence,
            "suggested_action": action,
            "confidence": c.confidence if c.confidence in CONFIDENCES else "MEDIUM",
        }
        if c.break_type == "COMPOUND":
            b["component_causes"] = sorted(set(c.component_causes))
        v.breaks.append(b)
    return v


def _supported_causes(a: Row, b: Row) -> set[str]:
    """The cause set arithmetic actually supports for a 1:1 correspondence."""
    causes = set()
    if a.gross_amount != b.gross_amount:
        if _fx_explains(a, b):
            causes.add("FX_DIFFERENCE")
        elif abs(a.gross_amount - b.gross_amount) <= TOLERANCE:
            causes.add("ROUNDING_DIFFERENCE")
        else:
            causes.add("AMOUNT_MISMATCH")
    if a.fee_amount != b.fee_amount:
        causes.add("FEE_MISMATCH")
    return causes


def _recompute(break_type: str, a_rows: list[Row], b_rows: list[Row]):
    """Authoritative amounts for a break, recomputed from source rows only."""
    sa = sum((r.net_amount for r in a_rows), Decimal("0")) if a_rows else None
    sb = sum((r.net_amount for r in b_rows), Decimal("0")) if b_rows else None

    if break_type == "FEE_MISMATCH" and len(a_rows) == 1 and len(b_rows) == 1:
        fa, fb = a_rows[0].fee_amount, b_rows[0].fee_amount
        return fa, fb, fa - fb
    if break_type in ("AMOUNT_MISMATCH", "FX_DIFFERENCE", "ROUNDING_DIFFERENCE") \
            and len(a_rows) == 1 and len(b_rows) == 1:
        ga, gb = a_rows[0].gross_amount, b_rows[0].gross_amount
        return ga, gb, ga - gb
    if break_type == "DUPLICATE":
        rows = b_rows or a_rows
        rep = rows[0]
        # The excess booked by the duplicate copies, not the whole group.
        excess = rep.net_amount * (len(rows) - 1)
        if b_rows:
            return None, rep.net_amount, excess
        return rep.net_amount, None, excess
    if break_type == "AMBIGUOUS":
        return sa, sb, (sa or Decimal("0")) - (sb or Decimal("0"))
    if break_type == "MISSING_IN_B":
        return sa, None, sa
    if break_type == "MISSING_IN_A":
        return None, sb, sb
    return sa, sb, (sa or Decimal("0")) - (sb or Decimal("0"))


# ---------------------------------------------------------------- runner


def solve_case(provider, data_dir: Path, case_id: str) -> tuple[dict, dict]:
    """Run all five stages for one case. Returns (output, case_meta)."""
    case_dir = data_dir / "cases" / case_id
    t0 = time.time()

    a_rows = normalize(case_dir / "source_a.csv")
    b_rows = normalize(case_dir / "source_b.csv")
    a_by_id = {r.row_id: r for r in a_rows}
    b_by_id = {r.row_id: r for r in b_rows}

    m = match_candidates(a_rows, b_rows)
    candidates = build_candidates(m)
    decisions, llm_meta = llm_interpret(provider, case_id, candidates, a_by_id, b_by_id)
    applied = apply_decisions(candidates, decisions)
    v = verify(candidates, a_by_id, b_by_id)

    matched_pairs = [{"a_row_id": a.row_id, "b_row_id": b.row_id}
                     for a, b in m.pairs
                     if classify_pair(a, b) is None]
    output = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "system": {"name": SYSTEM_NAME,
                   "model": getattr(provider, "model", provider.name),
                   "provider": provider.name},
        "matches": sorted(matched_pairs, key=lambda x: x["a_row_id"]),
        "breaks": v.breaks,
    }
    meta = {
        "case_id": case_id,
        "seconds": round(time.time() - t0, 2),
        "rows_a": len(a_rows),
        "rows_b": len(b_rows),
        "n_pairs": len(m.pairs),
        "n_candidates": len(candidates),
        "n_breaks_emitted": len(v.breaks),
        "llm": llm_meta,
        "llm_decisions_applied": applied,
        "verifier_corrections": v.corrections,
        "n_verifier_corrections": v.n_corrections,
    }
    return output, meta


def run(data_dir: Path, outputs_dir: Path) -> dict:
    provider = provider_from_env()
    outputs_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    meta = {"system": SYSTEM_NAME, "provider": provider.name,
            "model": getattr(provider, "model", provider.name), "cases": []}
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
    meta["n_llm_calls"] = sum(1 for x in meta["cases"] if x["llm"]["called"])
    meta["total_verifier_corrections"] = sum(x["n_verifier_corrections"] for x in meta["cases"])
    (outputs_dir / "_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return meta


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=str, default="data")
    p.add_argument("--outputs", type=str, default="outputs/solution")
    args = p.parse_args()
    m = run(Path(args.data), Path(args.outputs))
    print(f"agent complete: system={m['system']} provider={m['provider']} "
          f"cases={len(m['cases'])} llm_calls={m['n_llm_calls']} "
          f"verifier_corrections={m['total_verifier_corrections']} "
          f"total_seconds={m['total_seconds']}")
