"""Deterministic validation of extracted invoice values.

Severity vocabulary (directive 6B: distinguish confirmed inconsistencies from heuristic warnings):
  CONFIRMED  a deterministic rule is violated by the values as recorded (the document, as read, is
             inconsistent). Possible OCR error is still possible, hence a human must resolve it.
  HEURISTIC  plausibility signal that may be legitimate (rounding, mixed tax rates, low OCR confidence).

All money arithmetic uses Decimal. Rules read *current* values, so human corrections re-run them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from .extraction import SEVERITY_CONFIRMED, SEVERITY_HEURISTIC, ExtractionIssue

ROUNDING_TOLERANCE_PER_LINE = Decimal("0.01")
ROUND_OFF_LIMIT = Decimal("0.50")   # Indian invoices commonly round the grand total to the nearest rupee
LOW_CONFIDENCE = 0.80
KEY_FIELDS = ("invoice_number", "issue_date", "seller.name", "subtotal", "total_amount")

_ALPHA36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_LINE = re.compile(r"^line_items\[(\d+)\]\.(\w+)$")


def gstin_check_char(first14: str) -> str:
    """GSTIN check character (base-36, alternating weights 1/2, digit-sum in base 36).

    Verified against independent published GSTINs (see tests). The rule is a GSTN system convention
    followed by validators, not text in the CGST Act, so a failure means "cannot be a valid GSTIN as
    read" (often an OCR slip), not a legal finding.
    """
    total = 0
    for index, char in enumerate(first14):
        value = _ALPHA36.index(char) * (1 if index % 2 == 0 else 2)
        total += value // 36 + value % 36
    return _ALPHA36[(36 - total % 36) % 36]


def gstin_problem(value: str) -> Optional[str]:
    gstin = re.sub(r"\s+", "", value or "").upper()
    if len(gstin) != 15:
        return f"has {len(gstin)} characters instead of 15"
    if not _GSTIN_SHAPE.match(gstin):
        return "does not match the GSTIN structure (state code, PAN, entity code, 'Z', check character)"
    if gstin_check_char(gstin[:14]) != gstin[14]:
        return "fails the check-character test"
    return None


def money(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    cleaned = re.sub(r"[,\s₹]|INR|Rs\.?", "", str(value), flags=re.I)
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _iso(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def normalise_business_key(values: Mapping[str, str]) -> Optional[str]:
    """Stable duplicate key: seller identity | invoice number | issue date | total (2 dp)."""
    seller = ""
    for path, value in values.items():
        if re.fullmatch(r"seller\.tax_ids\[\d+\]\.value", path) and value:
            seller = re.sub(r"[^A-Z0-9]", "", value.upper())
            break
    if not seller:
        seller = re.sub(r"[^a-z0-9]", "", (values.get("seller.name") or "").lower())
    number = re.sub(r"[^A-Z0-9]", "", (values.get("invoice_number") or "").upper())
    issued = values.get("issue_date") or ""
    total = money(values.get("total_amount"))
    if not (seller and number and _iso(issued) and total is not None):
        return None
    return f"{seller}|{number}|{issued}|{total.quantize(Decimal('0.01'))}"


@dataclass(frozen=True)
class FieldMeta:
    confidence: Optional[float]
    location_status: str


def validate(
    values: Mapping[str, str],
    meta: Mapping[str, FieldMeta] | None = None,
    today: Optional[date] = None,
) -> list[ExtractionIssue]:
    """Run all deterministic checks over current field values."""
    meta = meta or {}
    today = today or date.today()
    issues: list[ExtractionIssue] = []

    def add(code, severity, message, path=None):
        issues.append(ExtractionIssue(code, severity, message, path))

    for path in ("invoice_number", "issue_date", "seller.name", "total_amount"):
        if not (values.get(path) or "").strip():
            add("MISSING_REQUIRED", SEVERITY_CONFIRMED, f"Required field '{path}' is missing", path)

    # --- per-line arithmetic
    lines: dict[int, dict[str, Decimal]] = {}
    for path, value in values.items():
        match = _LINE.match(path)
        if match and match.group(2) in ("quantity", "unit_price", "total"):
            number = money(value)
            if number is not None:
                lines.setdefault(int(match.group(1)), {})[match.group(2)] = number
    for index in sorted(lines):
        row = lines[index]
        if {"quantity", "unit_price", "total"} <= row.keys():
            expected = row["quantity"] * row["unit_price"]
            tolerance = max(ROUNDING_TOLERANCE_PER_LINE, abs(row["quantity"]) * Decimal("0.005"))
            if abs(expected - row["total"]) > tolerance:
                add("LINE_TOTAL_MISMATCH", SEVERITY_CONFIRMED,
                    f"Line {index + 1}: quantity x unit price = {expected:.2f} but line total is {row['total']:.2f}",
                    f"line_items[{index}].total")

    subtotal, tax = money(values.get("subtotal")), money(values.get("tax_amount")) or Decimal(0)
    discount = money(values.get("discount_amount")) or Decimal(0)
    shipping = money(values.get("shipping_amount")) or Decimal(0)
    total = money(values.get("total_amount"))
    line_totals = [row["total"] for _, row in sorted(lines.items()) if "total" in row]

    if subtotal is not None and line_totals:
        tolerance = ROUNDING_TOLERANCE_PER_LINE * len(line_totals)
        if abs(sum(line_totals) - subtotal) > tolerance:
            add("SUBTOTAL_MISMATCH", SEVERITY_CONFIRMED,
                f"Line totals add up to {sum(line_totals):.2f} but subtotal is {subtotal:.2f}", "subtotal")

    if subtotal is not None and total is not None:
        expected_total = subtotal - discount + shipping + tax
        diff = abs(expected_total - total)
        if diff > Decimal("0.01"):
            if diff <= ROUND_OFF_LIMIT:
                add("POSSIBLE_ROUND_OFF", SEVERITY_HEURISTIC,
                    f"Total differs from subtotal - discount + shipping + tax ({expected_total:.2f}) by {diff:.2f}, "
                    "consistent with a printed round-off", "total_amount")
            else:
                add("TOTAL_MISMATCH", SEVERITY_CONFIRMED,
                    f"Subtotal - discount + shipping + tax = {expected_total:.2f} but total is {total:.2f}",
                    "total_amount")

    rate = money(values.get("tax_rate_percent"))
    if rate is not None and subtotal is not None and values.get("tax_amount") is not None:
        expected_tax = (subtotal - discount) * rate / Decimal(100)
        if expected_tax != 0 and abs(expected_tax - tax) / abs(expected_tax) > Decimal("0.01") and abs(expected_tax - tax) > Decimal("1"):
            add("TAX_RATE_MISMATCH", SEVERITY_HEURISTIC,
                f"Tax {tax:.2f} differs from {rate}% of the taxable amount ({expected_tax:.2f}); "
                "mixed rates or CGST/SGST/IGST splits can explain this", "tax_amount")

    # --- identifiers and dates
    for path, value in values.items():
        if re.fullmatch(r"(seller|buyer)\.tax_ids\[\d+\]\.value", path):
            party_scheme = values.get(path.replace(".value", ".scheme"), "")
            if party_scheme == "gst" or re.fullmatch(r"[0-9]{2}[A-Za-z0-9]{13}", re.sub(r"\s+", "", value)):
                problem = gstin_problem(value)
                if problem:
                    add("GSTIN_INVALID", SEVERITY_CONFIRMED, f"GSTIN '{value}' {problem}", path)

    issued, due = _iso(values.get("issue_date")), _iso(values.get("due_date"))
    if issued and due and due < issued:
        add("DUE_BEFORE_ISSUE", SEVERITY_CONFIRMED, "Due date is earlier than the issue date", "due_date")
    if issued and issued > today + timedelta(days=1):
        add("DATE_IN_FUTURE", SEVERITY_HEURISTIC, f"Issue date {issued.isoformat()} is in the future", "issue_date")
    if values.get("issue_date") and not issued:
        add("DATE_UNPARSEABLE", SEVERITY_CONFIRMED, f"Issue date '{values['issue_date']}' is not a valid ISO date", "issue_date")

    # --- evidence quality on the fields that carry the money
    for path in KEY_FIELDS:
        m = meta.get(path)
        if m is None or path not in values:
            continue
        if m.location_status in ("unlocated", "conflicting"):
            add("CITATION_" + m.location_status.upper(), SEVERITY_HEURISTIC,
                f"'{path}' could not be tied to a single place on the page ({m.location_status})", path)
        elif m.confidence is not None and m.confidence < LOW_CONFIDENCE:
            add("LOW_CONFIDENCE", SEVERITY_HEURISTIC,
                f"'{path}' read with low confidence ({m.confidence:.2f})", path)
    return issues
