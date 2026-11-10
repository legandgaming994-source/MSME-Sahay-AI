"""The registry of facts the platform will accept, with validation.

Only facts listed here can be set, and each has a plain-language description that is shown to the user when
the fact is missing. `msme_category` is DERIVED from investment and turnover by the effective-dated rule engine
and can never be typed in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

UDYAM = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")
SECTORS = ("manufacturing", "service", "trading")


class FactError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class FactSpec:
    key: str
    type: str                 # bool | number | enum | udyam | text
    description: str
    choices: tuple[str, ...] = ()
    needs_evidence_hint: bool = False


SPECS: dict[str, FactSpec] = {s.key: s for s in (
    FactSpec("udyam_number", "udyam", "Udyam Registration Number (format UDYAM-XX-00-0000000). The format is checked; "
             "registration status is NOT verified by this platform."),
    FactSpec("udyam_registered", "bool", "Whether the enterprise holds a valid Udyam Registration"),
    FactSpec("sector", "enum", "Principal activity: manufacturing, service or trading", SECTORS),
    FactSpec("non_corporate_non_farm", "bool", "Non-corporate, non-farm business (proprietorship, partnership or similar; "
             "not a company, not primary agriculture)"),
    FactSpec("investment_inr", "number", "Investment in plant & machinery / equipment, in rupees", needs_evidence_hint=True),
    FactSpec("turnover_inr", "number", "Annual turnover, in rupees", needs_evidence_hint=True),
    FactSpec("credit_facility_amount", "number", "Amount of credit being sought, in rupees"),
    FactSpec("collateral_free_requested", "bool", "Whether the loan is sought without collateral or third-party guarantee"),
    FactSpec("prior_tarun_loan_repaid", "bool", "Whether a previous Tarun-category MUDRA loan was fully repaid"),
    FactSpec("loan_purpose", "text", "Purpose of the credit (working capital, equipment, expansion ...)"),
)}
DERIVED_KEYS = {"msme_category"}


def validate(key: str, value: Any) -> Any:
    """Return the canonical value for ``key`` or raise FactError."""
    if key in DERIVED_KEYS:
        raise FactError("derived_fact", f"'{key}' is derived from investment and turnover and cannot be set directly")
    spec = SPECS.get(key)
    if spec is None:
        raise FactError("unknown_fact", f"Unknown fact '{key}'")
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise FactError("invalid_value", f"'{key}' must be true or false")
        return value
    if spec.type == "number":
        if isinstance(value, bool):
            raise FactError("invalid_value", f"'{key}' must be a number")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise FactError("invalid_value", f"'{key}' must be a number")
        if not number.is_finite() or number < 0 or number > Decimal("1e13"):
            raise FactError("invalid_value", f"'{key}' must be between 0 and 10 trillion rupees")
        if key == "credit_facility_amount" and number == 0:
            raise FactError("invalid_value", "credit_facility_amount must be greater than zero")
        return int(number) if number == number.to_integral_value() else float(number)
    if spec.type == "enum":
        if value not in spec.choices:
            raise FactError("invalid_value", f"'{key}' must be one of: {', '.join(spec.choices)}")
        return value
    if spec.type == "udyam":
        text = str(value or "").strip().upper()
        if not UDYAM.match(text):
            raise FactError("invalid_udyam", "Udyam number must look like UDYAM-XX-00-0000000")
        return text
    text = str(value or "").strip()
    if not text or len(text) > 200:
        raise FactError("invalid_value", f"'{key}' must be 1-200 characters")
    return text


def describe(key: str) -> Optional[str]:
    spec = SPECS.get(key)
    if spec:
        return spec.description
    if key == "msme_category":
        return "Enterprise category (Micro/Small/Medium) - derived from investment_inr and turnover_inr"
    return None
