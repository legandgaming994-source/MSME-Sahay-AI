"""Draft preparation figures derived ONLY from approved documents.

Not a return and not tax advice: input-tax-credit eligibility, reverse charge, HSN splits and tax-head
(CGST/SGST/IGST) allocation are NOT computed. Anything that cannot be summed safely is listed under
`attention` instead of being silently included or dropped.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

from msme.documents.checks import money

DISCLAIMER = ("Draft preparation figures from approved documents only; this is not a return. Input tax credit "
              "eligibility, reverse charge and tax-head allocation are not assessed.")
_TWO = Decimal("0.01")


def period_range(period: str) -> tuple[date, date]:
    """'YYYY-MM' | 'YYYY-MM..YYYY-MM' | 'FYyyyy-yy' -> inclusive (first_day, last_day)."""
    def month_bounds(text: str) -> tuple[date, date]:
        y, m = int(text[:4]), int(text[5:7])
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])

    if re.fullmatch(r"\d{4}-\d{2}", period):
        return month_bounds(period)
    if re.fullmatch(r"\d{4}-\d{2}\.\.\d{4}-\d{2}", period):
        a, b = period.split("..")
        return month_bounds(a)[0], month_bounds(b)[1]
    m = re.fullmatch(r"FY(\d{4})-\d{2}", period)
    if m:
        y = int(m.group(1))
        return date(y, 4, 1), date(y + 1, 3, 31)
    raise ValueError(f"Unrecognised period {period!r}")


def _norm_gstin(value: Optional[str]) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _q(value: Decimal) -> str:
    return str(value.quantize(_TWO))


def load_approved_documents(db) -> dict[str, dict]:
    """All APPROVED documents with their current field values, keyed by document id."""
    rows = db.sql(
        'SELECT d.id, d.sha256, d.filename, d.document_type, d.duplicate_of, f.field_path, f.current_value '
        'FROM "MSME Document" d JOIN "MSME Extracted Field" f ON f.document_id = d.id '
        "WHERE d.status = 'approved' ORDER BY d.id, f.field_path")
    docs: dict[str, dict] = {}
    for r in rows:
        d = docs.setdefault(r["id"], {"id": r["id"], "sha256": r["sha256"], "filename": r["filename"],
                                      "type": r["document_type"], "duplicate_of": r["duplicate_of"], "values": {}})
        d["values"][r["field_path"]] = r["current_value"]
    return docs


def party_gstins(values: dict, party: str) -> set[str]:
    return {_norm_gstin(val) for p, val in values.items() if re.fullmatch(party + r"\.tax_ids\[\d+\]\.value", p)}


def direction_for(values: dict, company_gstin: Optional[str]) -> str:
    """'outward' if the company GSTIN is the seller, 'inward' if it is the buyer, else 'unclassified'."""
    gstin = _norm_gstin(company_gstin)
    if gstin and gstin in party_gstins(values, "seller"):
        return "outward"
    if gstin and gstin in party_gstins(values, "buyer"):
        return "inward"
    return "unclassified"


def compute_figures(db, company_gstin: Optional[str], period: str) -> dict:
    start, end = period_range(period)
    gstin = _norm_gstin(company_gstin)
    docs = load_approved_documents(db)

    attention: dict[str, list] = defaultdict(list)
    included: list[dict] = []
    totals = {"outward": [Decimal(0)] * 3 + [0], "inward": [Decimal(0)] * 3 + [0]}
    for d in sorted(docs.values(), key=lambda x: x["id"]):
        v = d["values"]
        try:
            issued = date.fromisoformat(v.get("issue_date") or "")
        except ValueError:
            continue
        if not (start <= issued <= end):
            continue
        if d["duplicate_of"]:
            attention["excluded_duplicates"].append(d["id"])
            continue
        if d["type"] == "credit_note":
            attention["credit_notes"].append(d["id"])
            continue
        if d["type"] != "invoice":
            continue
        if (v.get("currency") or "INR").upper() != "INR":
            attention["non_inr"].append(d["id"])
            continue
        subtotal, tax, total = (money(v.get(k)) for k in ("subtotal", "tax_amount", "total_amount"))
        if subtotal is None or total is None:
            attention["incomplete"].append(d["id"])
            continue
        tax = tax if tax is not None else Decimal(0)
        direction = direction_for(v, gstin)
        if direction == "unclassified":
            attention["unclassified"].append(d["id"])
            continue
        t = totals[direction]
        t[0] += subtotal; t[1] += tax; t[2] += total; t[3] += 1
        included.append({"id": d["id"], "filename": d["filename"], "invoice_number": v.get("invoice_number"),
                         "issue_date": issued.isoformat(), "direction": direction,
                         "subtotal": _q(subtotal), "tax": _q(tax), "total": _q(total)})

    def block(key):
        t = totals[key]
        return {"count": t[3], "taxable_value": _q(t[0]), "tax": _q(t[1]), "total": _q(t[2])}

    basis = [(x["id"], next(dd["sha256"] for dd in docs.values() if dd["id"] == x["id"]), x["direction"],
              x["subtotal"], x["tax"], x["total"]) for x in included]
    digest = hashlib.sha256(json.dumps(
        {"period": period, "gstin": gstin, "included": basis,
         "attention": {k: sorted(v) for k, v in attention.items()}}, sort_keys=True).encode()).hexdigest()
    return {"period": period, "from": start.isoformat(), "to": end.isoformat(), "gstin": gstin or None,
            "outward": block("outward"), "inward": block("inward"), "documents": included,
            "attention": {k: sorted(v) for k, v in attention.items()}, "digest": digest, "disclaimer": DISCLAIMER}
