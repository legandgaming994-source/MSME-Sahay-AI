"""Cross-check of DECLARED turnover against evidence the platform holds.

Every flag is HEURISTIC: the declared figure's period is unknown (financial year vs trailing 12 months), supplies may be
exempt or exports, credit notes are not netted, only approved documents count, and bank credits include loans and
transfers. A flag is a prompt to look, never an accusation and never a determination.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from msme.compliance.figures import compute_figures

_TWO = Decimal("0.01")
CAVEATS = (
    "The period the declared turnover refers to (financial year or trailing 12 months) is not recorded.",
    "Only approved outward invoices are counted, over the 12 complete calendar months before the current month; "
    "credit notes are listed but not netted, and exempt supplies and exports are not distinguished.",
    "Bank credits cover the trailing 365 days across this company's accounts and include loans, transfers and refunds.",
)


def _month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def turnover_check(db, company: str, as_of: date, declared: Optional[Decimal]) -> dict:
    out: dict = {"company": company, "as_of": as_of.isoformat(), "caveats": list(CAVEATS), "flags": []}
    if declared is None:
        return {**out, "status": "NO_DECLARATION", "declared_turnover": None,
                "message": "Enter turnover_inr (annual turnover) to run the cross-check."}
    profile = db.sql('SELECT gstin FROM "MSME Compliance Profile" WHERE company = ?', [company])
    gstin = profile[0]["gstin"] if profile else None
    declared = Decimal(str(declared))
    ey, em = _month_add(as_of.year, as_of.month, -1)
    sy, sm = _month_add(ey, em, -11)
    inv_from, inv_to = f"{sy}-{sm:02d}", f"{ey}-{em:02d}"
    window_start = as_of - timedelta(days=364)
    bank = db.sql(
        'SELECT COALESCE(SUM(t.deposit), 0) AS total, COUNT(*) AS n FROM "Bank Transaction" t '
        'JOIN "Bank Account" a ON t.bank_account_id = a.name WHERE a.company = ? AND t.docstatus <> 2 '
        "AND COALESCE(t.reversal_indicator, 0) = 0 AND COALESCE(t.status, '') <> 'Informational' "
        "AND COALESCE(t.currency, 'INR') = 'INR' AND t.deposit > 0 AND t.posting_date >= ? AND t.posting_date <= ?",
        [company, window_start.isoformat(), as_of.isoformat()])
    deposits = Decimal(str(round(float(bank[0]["total"] or 0), 2))).quantize(_TWO)
    documented: Optional[Decimal] = None
    if gstin:
        documented = Decimal(compute_figures(db, gstin, f"{inv_from}..{inv_to}")["outward"]["taxable_value"])
    out.update(status="CHECKED", declared_turnover=str(declared.quantize(_TWO)),
               invoice_window={"from": inv_from, "to": inv_to}, bank_window={"from": window_start.isoformat(), "to": as_of.isoformat()},
               documented_outward_taxable=None if documented is None else str(documented),
               bank_deposits=str(deposits), bank_deposit_count=bank[0]["n"])
    flags = out["flags"]
    if documented is None:
        flags.append({"code": "NO_GSTIN", "severity": "INFO", "message":
                      "Without the company GSTIN in the compliance profile, invoices cannot be classified as sales."})
    elif declared > 0:
        ratio = documented / declared
        out["documented_ratio"] = round(float(ratio), 4)
        if documented > declared + 1:
            flags.append({"code": "DOCUMENTED_EXCEEDS_DECLARED", "severity": "HEURISTIC", "message":
                          f"Approved invoices total {documented:.2f}, which is more than the declared turnover {declared:.2f}. "
                          "The declaration may be understated or may refer to a different period."})
        elif ratio < Decimal("0.5"):
            flags.append({"code": "LOW_DOCUMENT_COVERAGE", "severity": "HEURISTIC", "message":
                          f"Approved invoices evidence only {ratio:.0%} of the declared turnover; upload and approve more "
                          "invoices, or check the declaration."})
    if declared > 0:
        bratio = deposits / declared
        out["bank_ratio"] = round(float(bratio), 4)
        if bratio < Decimal("0.5"):
            flags.append({"code": "LOW_BANK_CREDITS", "severity": "HEURISTIC", "message":
                          f"Bank credits total {deposits:.2f} ({bratio:.0%} of declared turnover). Other bank accounts or "
                          "cash receipts may explain this."})
    return out
