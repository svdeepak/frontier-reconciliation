"""Deterministic synthetic reconciliation data generator (M2).

Generates 14 independent evaluation cases (Source A CSV, Source B CSV,
ground_truth.json) plus a manifest. Byte-identical output for the same
MASTER_SEED and GENERATOR_VERSION. Stdlib only.

Usage: python -m recon.generate [--seed 42] [--out data]
"""

import argparse
import csv
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

from recon.taxonomy import SCHEMA_VERSION

GENERATOR_VERSION = "1.0"
MASTER_SEED = 42
CENT = Decimal("0.01")

CURRENCIES = ["USD", "EUR", "GBP", "INR", "AED"]
REF_PREFIXES = ["INV", "PAY", "RMT", "TXN"]
DESCS_A = ["Remittance payout", "Merchant settlement", "Refund", "Payout batch item", "Card settlement"]
DESCS_B = ["PAYOUT", "SETTLEMENT", "REFUND", "BATCH ITEM", "CARD STL"]


def d2(x) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Txn:
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

    @property
    def net_amount(self) -> Decimal:
        return self.gross_amount - self.fee_amount

    def row(self) -> list[str]:
        return [
            self.row_id, self.date, self.reference, self.description, self.currency,
            f"{self.fx_rate:.4f}" if self.fx_rate is not None else "",
            self.foreign_currency or "",
            f"{self.foreign_amount:.2f}" if self.foreign_amount is not None else "",
            f"{self.gross_amount:.2f}", f"{self.fee_amount:.2f}", f"{self.net_amount:.2f}",
        ]


HEADER = ["row_id", "date", "reference", "description", "currency", "fx_rate",
          "foreign_currency", "foreign_amount", "gross_amount", "fee_amount", "net_amount"]


def messy_ref(rng: random.Random, ref: str) -> str:
    """B-side reference noise: benign, never destroys identity."""
    style = rng.choice(["lower", "space", "prefix", "same"])
    if style == "lower":
        return ref.lower()
    if style == "space":
        return ref.replace("-", " ")
    if style == "prefix":
        return "PB/" + ref
    return ref


@dataclass
class CaseBuilder:
    case_id: str
    seed: int
    description: str
    rng: random.Random = field(init=False)
    a_rows: list[Txn] = field(default_factory=list)
    b_rows: list[Txn] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)
    breaks: list[dict] = field(default_factory=list)
    _n: int = 0

    def __post_init__(self):
        self.rng = random.Random(self.seed)

    def ids(self) -> tuple[str, str]:
        self._n += 1
        return f"A{1000 + self._n}", f"B{5000 + self._n}"

    def base_pair(self, fx: bool = False) -> tuple[Txn, Txn]:
        """One clean matched A/B pair (identical amounts; benign ref/date noise)."""
        aid, bid = self.ids()
        rng = self.rng
        ref = f"{rng.choice(REF_PREFIXES)}-{rng.randrange(10000, 99999)}"
        day = date(2026, 8, 1) + timedelta(days=rng.randrange(0, 20))
        cur = rng.choice(CURRENCIES)
        i = rng.randrange(len(DESCS_A))
        if fx:
            fcur = rng.choice([c for c in CURRENCIES if c != cur])
            famt = d2(Decimal(rng.randrange(20000, 500000)) / 100)
            rate = (Decimal(rng.randrange(8000, 12000)) / 10000).quantize(Decimal("0.0001"))
            gross = (famt * rate).quantize(CENT, rounding=ROUND_HALF_UP)
        else:
            fcur, famt, rate = None, None, None
            gross = d2(Decimal(rng.randrange(2000, 100000)) / 100)
        fee = d2(gross * Decimal("0.01"))
        a = Txn(aid, day.isoformat(), ref, DESCS_A[i], cur, rate, fcur, famt, gross, fee)
        bday = day + timedelta(days=rng.randrange(0, 2))
        b = Txn(bid, bday.isoformat(), messy_ref(rng, ref), DESCS_B[i], cur, rate, fcur, famt, gross, fee)
        return a, b

    def add_clean(self, n: int, fx_every: int = 0):
        for k in range(n):
            a, b = self.base_pair(fx=bool(fx_every) and k % fx_every == 0)
            self.a_rows.append(a)
            self.b_rows.append(b)
            self.matches.append({"a_row_id": a.row_id, "b_row_id": b.row_id})

    def add_break(self, break_type: str, a_ids: list[str], b_ids: list[str],
                  expected_difference: Decimal | None, notes: str,
                  component_causes: list[str] | None = None):
        br = {
            "break_id": f"GT-{len(self.breaks) + 1:03d}",
            "break_type": break_type,
            "a_row_ids": sorted(a_ids),
            "b_row_ids": sorted(b_ids),
            "expected_difference": float(expected_difference) if expected_difference is not None else None,
            "notes": notes,
        }
        if component_causes:
            br["component_causes"] = sorted(component_causes)
        self.breaks.append(br)

    # ---- planted break helpers (ground truth emitted by construction) ----

    def plant_missing_in_b(self):
        a, _ = self.base_pair()
        self.a_rows.append(a)
        self.add_break("MISSING_IN_B", [a.row_id], [], a.net_amount,
                       f"A row {a.row_id} (net {a.net_amount}) has no B counterpart.")
        return a

    def plant_missing_in_a(self):
        _, b = self.base_pair()
        self.b_rows.append(b)
        self.add_break("MISSING_IN_A", [], [b.row_id], b.net_amount,
                       f"B row {b.row_id} (net {b.net_amount}) has no A counterpart.")
        return b

    def plant_amount_mismatch(self, delta: Decimal):
        a, b = self.base_pair()
        b.gross_amount = d2(b.gross_amount + delta)
        self.a_rows.append(a)
        self.b_rows.append(b)
        self.add_break("AMOUNT_MISMATCH", [a.row_id], [b.row_id], delta,
                       f"Gross differs by {delta}: A {a.gross_amount} vs B {b.gross_amount}.")

    def plant_fee_mismatch(self, delta: Decimal):
        a, b = self.base_pair()
        b.fee_amount = d2(b.fee_amount + delta)
        self.a_rows.append(a)
        self.b_rows.append(b)
        self.add_break("FEE_MISMATCH", [a.row_id], [b.row_id], delta,
                       f"Fee differs by {delta}: A {a.fee_amount} vs B {b.fee_amount}.")

    def plant_fx_difference(self):
        """Same foreign amount and rate; A rounds HALF_UP, B rounds DOWN.

        Foreign amounts are chosen so the raw product has a third decimal
        digit >= 5, guaranteeing a 0.01 divergence between rounding modes.
        """
        a, b = self.base_pair(fx=True)
        while (a.foreign_amount * a.fx_rate).quantize(CENT, ROUND_HALF_UP) == \
              (a.foreign_amount * a.fx_rate).quantize(CENT, ROUND_DOWN):
            a, b = self.base_pair(fx=True)
        b.gross_amount = (b.foreign_amount * b.fx_rate).quantize(CENT, rounding=ROUND_DOWN)
        diff = a.gross_amount - b.gross_amount
        self.a_rows.append(a)
        self.b_rows.append(b)
        self.add_break("FX_DIFFERENCE", [a.row_id], [b.row_id], diff,
                       f"Same {a.foreign_currency} {a.foreign_amount} @ {a.fx_rate}: "
                       f"A HALF_UP {a.gross_amount}, B DOWN {b.gross_amount}.")

    def plant_rounding_difference(self):
        a, b = self.base_pair()
        b.gross_amount = d2(b.gross_amount - CENT)
        self.a_rows.append(a)
        self.b_rows.append(b)
        self.add_break("ROUNDING_DIFFERENCE", [a.row_id], [b.row_id], CENT,
                       f"One-cent difference within tolerance: A {a.gross_amount} vs B {b.gross_amount}.")

    def plant_duplicate_in_b(self):
        a, b = self.base_pair()
        dup_id = f"B{5000 + self._n}D"
        dup = Txn(dup_id, b.date, b.reference, b.description, b.currency,
                  b.fx_rate, b.foreign_currency, b.foreign_amount, b.gross_amount, b.fee_amount)
        self.a_rows.append(a)
        self.b_rows.extend([b, dup])
        self.matches.append({"a_row_id": a.row_id, "b_row_id": b.row_id})
        self.add_break("DUPLICATE", [], [b.row_id, dup_id], b.net_amount,
                       f"B rows {b.row_id}/{dup_id} book the same economic transaction twice.")

    def plant_ambiguous(self):
        """Two same-day, same-amount A rows vs two garbled B rows: no basis to pair."""
        a1, b1 = self.base_pair()
        a2id, b2id = self.ids()
        a2 = Txn(a2id, a1.date, f"{a1.reference}X", a1.description, a1.currency,
                 a1.fx_rate, a1.foreign_currency, a1.foreign_amount, a1.gross_amount, a1.fee_amount)
        b1.reference = "REF ILLEGIBLE"
        b2 = Txn(b2id, b1.date, "REF MISSING", b1.description, b1.currency,
                 b1.fx_rate, b1.foreign_currency, b1.foreign_amount, b1.gross_amount, b1.fee_amount)
        self.a_rows.extend([a1, a2])
        self.b_rows.extend([b1, b2])
        self.add_break("AMBIGUOUS", [a1.row_id, a2.row_id], [b1.row_id, b2.row_id], Decimal("0"),
                       "Two identical-amount same-day rows on each side; references unusable; "
                       "correspondence cannot be established from data.")

    def plant_compound_fee_fx(self, fee_delta: Decimal):
        """FX rounding divergence AND fee mismatch on the same correspondence."""
        aid, bid = self.ids()
        famt, rate = Decimal("1234.50"), Decimal("1.0857")
        # 1234.50 * 1.0857 = 1340.29665 -> HALF_UP 1340.30, DOWN 1340.29
        gross_a = (famt * rate).quantize(CENT, ROUND_HALF_UP)
        gross_b = (famt * rate).quantize(CENT, ROUND_DOWN)
        assert gross_a - gross_b == CENT, "FX construction must diverge by exactly one cent"
        a = Txn(aid, "2026-08-10", "RMT-70001", "Remittance payout", "USD",
                rate, "EUR", famt, gross_a, Decimal("2.50"))
        b = Txn(bid, "2026-08-11", "rmt 70001", "PAYOUT", "USD",
                rate, "EUR", famt, gross_b, Decimal("2.50") + fee_delta)
        self.a_rows.append(a)
        self.b_rows.append(b)
        diff = a.net_amount - b.net_amount  # 0.61 when fee_delta=0.60
        self.add_break("COMPOUND", [aid], [bid], diff,
                       f"FX: {famt} EUR @ {rate} -> A HALF_UP {gross_a} vs B DOWN {gross_b} (-0.01); "
                       f"fee A 2.50 vs B {b.fee_amount} (+{fee_delta}); net diff {diff}.",
                       component_causes=["FEE_MISMATCH", "FX_DIFFERENCE"])
        return diff

    # ---- output ----

    def totals(self) -> dict:
        return {
            "a_gross": str(sum(t.gross_amount for t in self.a_rows)),
            "b_gross": str(sum(t.gross_amount for t in self.b_rows)),
            "a_net": str(sum(t.net_amount for t in self.a_rows)),
            "b_net": str(sum(t.net_amount for t in self.b_rows)),
        }

    def write(self, out: Path) -> dict:
        case_dir = out / "cases" / self.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in (("source_a.csv", self.a_rows), ("source_b.csv", self.b_rows)):
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(HEADER)
            for t in rows:
                w.writerow(t.row())
            (case_dir / name).write_text(buf.getvalue())
        gt = {
            "schema_version": SCHEMA_VERSION,
            "case_id": self.case_id,
            "seed": self.seed,
            "description": self.description,
            "matches": self.matches,
            "breaks": self.breaks,
        }
        (case_dir / "ground_truth.json").write_text(json.dumps(gt, indent=2, sort_keys=True) + "\n")
        t = self.totals()
        by_type: dict[str, int] = {}
        for br in self.breaks:
            by_type[br["break_type"]] = by_type.get(br["break_type"], 0) + 1
        return {
            "case_id": self.case_id,
            "seed": self.seed,
            "description": self.description,
            "rows_a": len(self.a_rows),
            "rows_b": len(self.b_rows),
            "n_breaks": len(self.breaks),
            "breaks_by_type": by_type,
            "totals": t,
            "aggregate_gross_equal": t["a_gross"] == t["b_gross"],
            "aggregate_net_equal": t["a_net"] == t["b_net"],
        }


def build_cases(master_seed: int) -> list[CaseBuilder]:
    def cb(i: int, cid: str, desc: str) -> CaseBuilder:
        return CaseBuilder(cid, master_seed * 1000 + i, desc)

    cases: list[CaseBuilder] = []

    c = cb(1, "case_01_clean_small", "All rows match; no breaks. False-positive pressure.")
    c.add_clean(12)
    cases.append(c)

    c = cb(2, "case_02_clean_messy", "All rows match; heavy reference noise and date shifts. False-positive pressure.")
    c.add_clean(16, fx_every=4)
    cases.append(c)

    c = cb(3, "case_03_missing_in_b", "Two A rows absent from B.")
    c.add_clean(10)
    c.plant_missing_in_b()
    c.plant_missing_in_b()
    cases.append(c)

    c = cb(4, "case_04_missing_in_a", "Two B rows absent from A.")
    c.add_clean(10)
    c.plant_missing_in_a()
    c.plant_missing_in_a()
    cases.append(c)

    c = cb(5, "case_05_amount_mismatch", "Two amount discrepancies beyond tolerance.")
    c.add_clean(10)
    c.plant_amount_mismatch(Decimal("12.40"))
    c.plant_amount_mismatch(Decimal("-3.75"))
    cases.append(c)

    c = cb(6, "case_06_fee_mismatch", "Two fee discrepancies.")
    c.add_clean(10)
    c.plant_fee_mismatch(Decimal("0.60"))
    c.plant_fee_mismatch(Decimal("1.25"))
    cases.append(c)

    c = cb(7, "case_07_amount_and_fee", "One amount and one fee discrepancy on separate rows.")
    c.add_clean(10)
    c.plant_amount_mismatch(Decimal("5.00"))
    c.plant_fee_mismatch(Decimal("-0.40"))
    cases.append(c)

    c = cb(8, "case_08_fx_difference", "Two FX rounding-mode divergences (HALF_UP vs DOWN).")
    c.add_clean(8, fx_every=3)
    c.plant_fx_difference()
    c.plant_fx_difference()
    cases.append(c)

    c = cb(9, "case_09_rounding", "Two one-cent differences within tolerance.")
    c.add_clean(10)
    c.plant_rounding_difference()
    c.plant_rounding_difference()
    cases.append(c)

    c = cb(10, "case_10_duplicates", "Two duplicated transactions in B.")
    c.add_clean(10)
    c.plant_duplicate_in_b()
    c.plant_duplicate_in_b()
    cases.append(c)

    c = cb(11, "case_11_ambiguous", "Identical-amount same-day rows with unusable references.")
    c.add_clean(8)
    c.plant_ambiguous()
    cases.append(c)

    c = cb(12, "case_12_compound", "Fee mismatch and FX difference on the same correspondence.")
    c.add_clean(10)
    c.plant_compound_fee_fx(Decimal("0.85"))
    cases.append(c)

    c = cb(13, "case_13_signature_adversarial",
           "Signature adversarial case: compound FX+fee break (net -0.61 on B), offset exactly by a "
           "duplicate in B (gross 177.51 / net 175.61) and a missing-in-B row (gross 177.50 / net 175.00). "
           "Aggregate gross and net totals are EQUAL while ground truth contains 3 row-level breaks.")
    c.add_clean(10)
    diff = c.plant_compound_fee_fx(Decimal("0.60"))
    assert diff == Decimal("0.61"), f"signature compound diff drifted: {diff}"
    # Duplicate in B: gross 177.51, fee 1.90, net 175.61  (adds +177.51 gross / +175.61 net to B)
    aid, bid = c.ids()
    a_dup = Txn(aid, "2026-08-12", "INV-70055", "Merchant settlement", "USD",
                None, None, None, Decimal("177.51"), Decimal("1.90"))
    b_dup1 = Txn(bid, "2026-08-12", "inv 70055", "SETTLEMENT", "USD",
                 None, None, None, Decimal("177.51"), Decimal("1.90"))
    b_dup2 = Txn(bid + "D", "2026-08-13", "PB/INV-70055", "SETTLEMENT", "USD",
                 None, None, None, Decimal("177.51"), Decimal("1.90"))
    c.a_rows.append(a_dup)
    c.b_rows.extend([b_dup1, b_dup2])
    c.matches.append({"a_row_id": aid, "b_row_id": bid})
    c.add_break("DUPLICATE", [], [bid, bid + "D"], Decimal("175.61"),
                "Same economic transaction booked twice in B; inflates B totals by gross 177.51 / net 175.61.")
    # Missing in B: gross 177.50, fee 2.50, net 175.00 (removes -177.50 gross / -175.00 net from B)
    aid2, _ = c.ids()
    a_miss = Txn(aid2, "2026-08-14", "PAY-70090", "Payout batch item", "USD",
                 None, None, None, Decimal("177.50"), Decimal("2.50"))
    c.a_rows.append(a_miss)
    c.add_break("MISSING_IN_B", [aid2], [], Decimal("175.00"),
                "A row has no B counterpart; reduces B totals by gross 177.50 / net 175.00.")
    cases.append(c)

    c = cb(14, "case_14_aggregate_trap",
           "Aggregate totals equal on gross and net while two offsetting amount errors (+0.75 / -0.75) exist.")
    c.add_clean(10)
    c.plant_amount_mismatch(Decimal("0.75"))
    c.plant_amount_mismatch(Decimal("-0.75"))
    cases.append(c)

    return cases


def generate(master_seed: int = MASTER_SEED, out_dir: str = "data") -> dict:
    out = Path(out_dir)
    cases = build_cases(master_seed)
    entries = [c.write(out) for c in cases]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "master_seed": master_seed,
        "n_cases": len(entries),
        "cases": entries,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=MASTER_SEED)
    p.add_argument("--out", type=str, default="data")
    args = p.parse_args()
    m = generate(args.seed, args.out)
    print(f"generated {m['n_cases']} cases -> {args.out}/ (seed={args.seed}, gen v{GENERATOR_VERSION})")
