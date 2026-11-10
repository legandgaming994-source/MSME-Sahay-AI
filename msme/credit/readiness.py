"""Financial-document readiness: what a lender will typically ask for, and what the platform can actually evidence.

Status vocabulary (never a composite score):
  EVIDENCED      the platform holds machine evidence (e.g. imported bank statements cover the window)
  PARTIAL        some evidence, with the gaps listed
  USER_RECORDED  derived from entries the user made (e.g. filings they recorded); not verified externally
  USER_ATTESTED  the user states they have it (optionally linking an uploaded document); not verified
  MISSING        nothing supplied
  UNKNOWN        the platform cannot tell yet (e.g. no compliance profile)
  NOT_APPLICABLE the user says it does not apply

The REQUIREMENTS themselves are typical practice, labelled UNVERIFIED_TYPICAL: lender requirements vary and this is
not an official list.
"""

from __future__ import annotations

import calendar
import json
from datetime import date, timedelta
from typing import Any, Optional

from msme.compliance.figures import compute_figures

NOTICE = ("Typical documents lenders ask for, not an official or lender-specific list. Requirements vary by "
          "lender and product; confirm with the lender.")
BASIS = "UNVERIFIED_TYPICAL"
WINDOW_DAYS = 365
GST_FORMS = ("GSTR-1", "GSTR-3B", "CMP-08", "GSTR-4")

ITEMS = (
    {"key": "udyam_registration", "label": "Udyam registration certificate", "attestable": True,
     "why": "Proves MSME status; most government credit schemes and priority-sector lending start from it."},
    {"key": "bank_statements", "label": "Bank statements for the last 12 months", "attestable": False,
     "why": "Shows cash flow and repayment capacity; lenders usually ask for 6-12 months."},
    {"key": "gst_return_history", "label": "GST return filing history", "attestable": False,
     "why": "Shows turnover consistency and compliance; lenders often cross-check turnover against returns."},
    {"key": "sales_invoice_evidence", "label": "Sales invoice evidence (monthly)", "attestable": False,
     "why": "Supports declared turnover with primary documents."},
    {"key": "financial_statements", "label": "Financial statements (balance sheet and profit & loss)", "attestable": True,
     "why": "Core of a credit appraisal for term loans and larger working-capital limits."},
    {"key": "income_tax_returns", "label": "Income-tax returns with computation", "attestable": True,
     "why": "Independent evidence of declared income, usually for the last 2-3 years."},
    {"key": "kyc_and_business_proof", "label": "KYC of the applicant and proof of business address", "attestable": True,
     "why": "Identity and existence checks are mandatory for any loan."},
    {"key": "purpose_and_quotation", "label": "Purpose of the loan with a quotation or project note", "attestable": True,
     "why": "Equipment and term loans are sanctioned against a stated purpose and supplier quotation."},
)
ATTESTABLE = {i["key"] for i in ITEMS if i["attestable"]}


def _merge(intervals: list[tuple[date, date]]) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1] + timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def bank_statement_coverage(db, company: str, as_of: date, window_days: int = WINDOW_DAYS) -> dict:
    """Days of the trailing window covered by non-cancelled imported statements (union of date ranges)."""
    start = as_of - timedelta(days=window_days - 1)
    rows = db.sql('SELECT from_date, to_date FROM "Bank Statement Import" WHERE company = ? AND docstatus <> 2 '
                  "AND from_date IS NOT NULL AND to_date IS NOT NULL", [company])
    spans = []
    for r in rows:
        try:
            a, b = date.fromisoformat(r["from_date"][:10]), date.fromisoformat(r["to_date"][:10])
        except ValueError:
            continue
        a, b = max(a, start), min(b, as_of)
        if a <= b:
            spans.append((a, b))
    merged = _merge(spans)
    covered = sum((b - a).days + 1 for a, b in merged)
    gaps, cursor = [], start
    for a, b in merged:
        if a > cursor:
            gaps.append((cursor, a - timedelta(days=1)))
        cursor = b + timedelta(days=1)
    if cursor <= as_of:
        gaps.append((cursor, as_of))
    latest = db.sql('SELECT MAX(to_date) AS d FROM "Bank Statement Import" WHERE company = ? AND docstatus <> 2', [company])
    return {"window_from": start.isoformat(), "window_to": as_of.isoformat(), "window_days": window_days,
            "covered_days": covered, "coverage": round(covered / window_days, 4),
            "gaps": [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days + 1} for a, b in gaps[:8]],
            "latest_statement_to": (latest[0]["d"][:10] if latest and latest[0]["d"] else None),
            "statements": len(rows)}


def _month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def gst_history(db, company: str, as_of: date) -> dict:
    start = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = db.sql('SELECT form, period, due_date, status FROM "MSME Compliance Task" WHERE company = ? '
                  "AND due_date >= ? AND due_date <= ? AND optional = 0 ORDER BY due_date", [company, start, as_of.isoformat()])
    rows = [r for r in rows if r["form"] in GST_FORMS]
    recorded = [r for r in rows if r["status"] == "filing_recorded"]
    missing = [{"form": r["form"], "period": r["period"], "due_date": r["due_date"]} for r in rows
               if r["status"] != "filing_recorded"]
    return {"due": len(rows), "recorded": len(recorded), "unrecorded": missing[:12], "unrecorded_count": len(missing)}


def invoice_month_coverage(db, company_gstin: Optional[str], as_of: date) -> dict:
    """Months (the 12 complete months before as_of's month) with at least one approved outward invoice."""
    ey, em = _month_add(as_of.year, as_of.month, -1)
    sy, sm = _month_add(ey, em, -11)
    months = [f"{y}-{m:02d}" for y, m in (_month_add(sy, sm, i) for i in range(12))]
    figures = compute_figures(db, company_gstin, f"{months[0]}..{months[-1]}")
    present = {d["issue_date"][:7] for d in figures["documents"] if d["direction"] == "outward"}
    return {"months": months, "months_with_documents": sorted(present & set(months)),
            "missing_months": [m for m in months if m not in present],
            "unclassified_documents": len(figures["attention"].get("unclassified", []))}


def build_readiness(db, company: str, as_of: date, current_facts: dict, attestations: dict) -> dict:
    """``current_facts``: key -> fact row; ``attestations``: item_key -> {status, note, document_id, entered_by, entered_at}."""
    profile = db.sql('SELECT gstin FROM "MSME Compliance Profile" WHERE company = ?', [company])
    gstin = profile[0]["gstin"] if profile else None
    items: list[dict] = []

    def add(base, status, evidence, missing=None):
        items.append({"key": base["key"], "label": base["label"], "why_it_matters": base["why"],
                      "requirement_basis": BASIS, "status": status, "evidence": evidence,
                      "what_is_missing": missing})

    for base in ITEMS:
        key, att = base["key"], attestations.get(base["key"])
        if key == "udyam_registration":
            number, registered = current_facts.get("udyam_number"), current_facts.get("udyam_registered")
            ev = []
            if number:
                ev.append({"type": "user_fact", "ref": "udyam_number", "detail": f"{json.loads(number['value_json'])} "
                           "(format checked; registration not verified)"})
            if registered and json.loads(registered["value_json"]) is True and number:
                add(base, "USER_ATTESTED", ev)
            elif registered and json.loads(registered["value_json"]) is False:
                add(base, "MISSING", [], "No Udyam registration. Registration is free on the government Udyam portal.")
            elif att and att["status"] == "provided":
                add(base, "USER_ATTESTED", [{"type": "attestation", "ref": att["entered_by"], "detail": att["note"]}])
            else:
                add(base, "UNKNOWN", ev, "State whether you hold Udyam registration and enter the number.")
        elif key == "bank_statements":
            cov = bank_statement_coverage(db, company, as_of)
            ev = [{"type": "bank_statement_imports", "ref": f"{cov['statements']} import(s)", "detail": cov}]
            if cov["statements"] == 0 or cov["covered_days"] == 0:
                add(base, "MISSING", [], "No imported bank statements in the last 12 months. Import CAMT statements.")
            elif cov["coverage"] >= 0.98:
                add(base, "EVIDENCED", ev)
            else:
                add(base, "PARTIAL", ev, f"Statements cover {cov['covered_days']} of {cov['window_days']} days; "
                    f"{len(cov['gaps'])} gap(s) listed in the evidence.")
        elif key == "gst_return_history":
            g = gst_history(db, company, as_of)
            ev = [{"type": "compliance_tasks", "ref": "filings recorded by the user (unverified)", "detail": g}]
            if g["due"] == 0:
                add(base, "UNKNOWN", [], "No GST return deadlines are tracked for this period. Set the compliance profile and "
                    "generate tasks, then record filings as you make them.")
            elif g["unrecorded_count"] == 0:
                add(base, "USER_RECORDED", ev)
            else:
                add(base, "PARTIAL", ev, f"{g['unrecorded_count']} of {g['due']} due returns have no filing recorded.")
        elif key == "sales_invoice_evidence":
            if not gstin:
                add(base, "UNKNOWN", [], "Set the company GSTIN in the compliance profile so invoices can be classified as sales.")
            else:
                c = invoice_month_coverage(db, gstin, as_of)
                ev = [{"type": "approved_documents", "ref": "approved outward invoices", "detail": c}]
                n = len(c["months_with_documents"])
                if n == 0:
                    add(base, "MISSING", [], "No approved outward invoices in the last 12 months. Upload and approve invoices.")
                elif n == 12:
                    add(base, "EVIDENCED", ev)
                else:
                    add(base, "PARTIAL", ev, f"Approved outward invoices exist for {n} of 12 months; missing: "
                        + ", ".join(c["missing_months"]))
        else:
            if att is None:
                add(base, "MISSING", [], "Not yet provided. Upload it as a document and record it here, or mark it not applicable.")
            elif att["status"] == "not_applicable":
                add(base, "NOT_APPLICABLE", [{"type": "attestation", "ref": att["entered_by"], "detail": att["note"]}])
            else:
                ev = [{"type": "attestation", "ref": att["entered_by"], "detail": att["note"]}]
                if att.get("document_id"):
                    ev.append({"type": "document", "ref": att["document_id"], "detail": "linked by the user; contents not verified"})
                add(base, "USER_ATTESTED", ev)

    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"company": company, "as_of": as_of.isoformat(), "items": items, "summary": counts,
            "notice": NOTICE, "requirement_basis": BASIS}
