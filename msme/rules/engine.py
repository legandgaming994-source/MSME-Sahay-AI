"""Deterministic, effective-dated rule engine for MSME classification, GST
deadlines and government-scheme criteria.

Design rules (see docs in PASS1 report, decision D4):
  * Every rule lives in versioned JSON under ``data/`` with an effective window
    and a source record; nothing time-sensitive is hard-coded here.
  * Unknown inputs yield explicit UNKNOWN / INSUFFICIENT_EVIDENCE results.
  * Nothing here claims an official filing, approval or lender decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

DATA_DIR = Path(__file__).parent / "data"
CATEGORIES = ("Micro", "Small", "Medium")

CRITERION_MET = "MET"
CRITERION_NOT_MET = "NOT_MET"
CRITERION_UNKNOWN = "UNKNOWN"

ASSESS_NOT_MET = "CRITERIA_NOT_MET"
ASSESS_APPEARS_MET = "CRITERIA_APPEARS_MET"
ASSESS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


class RuleError(Exception):
    """Raised when no rule version applies or inputs are structurally invalid."""


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _d(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def select_version(versions: Sequence[dict], as_of: date, what: str) -> dict:
    """Return the single rule version effective on ``as_of`` (inclusive window)."""
    matches = [
        v for v in versions
        if (_d(v.get("effective_from")) is None or _d(v["effective_from"]) <= as_of)
        and (_d(v.get("effective_to")) is None or as_of <= _d(v["effective_to"]))
    ]
    if not matches:
        raise RuleError(f"No {what} rule version is effective on {as_of.isoformat()}")
    if len(matches) > 1:
        raise RuleError(f"Overlapping {what} rule versions on {as_of.isoformat()}")
    return matches[0]


def _money(value: Any, label: str) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise RuleError(f"{label} is not a number: {value!r}") from exc
    if amount < 0:
        raise RuleError(f"{label} cannot be negative")
    return amount


# ---------------------------------------------------------------------------
# MSME classification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassificationResult:
    status: str                      # CLASSIFIED | NOT_MSME | INSUFFICIENT_EVIDENCE
    category: Optional[str]
    minimum_category: Optional[str]  # lower bound when evidence is partial
    rule_version: str
    source: dict
    explanation: tuple[str, ...]


def _metric_rank(value: Decimal, limits: dict, metric: str) -> int:
    for rank, category in enumerate(CATEGORIES):
        if value <= Decimal(limits[category][metric]):
            return rank
    return len(CATEGORIES)


def classify_msme(investment_inr: Any, turnover_inr: Any, as_of: date) -> ClassificationResult:
    """Composite classification: an enterprise sits in the highest category implied
    by EITHER metric (it must satisfy both limits of its category).

    Not modelled: Section 8(6) fiscal-year-end grace for reclassification and any
    turnover exclusions (e.g. exports); callers must apply those from source facts.
    """
    data = _load("msme_classification")
    version = select_version(data["versions"], as_of, "MSME classification")
    limits = version["limits_inr"]
    inv = _money(investment_inr, "investment")
    turn = _money(turnover_inr, "turnover")
    notes: list[str] = []

    ranks: dict[str, int] = {}
    if inv is not None:
        ranks["investment"] = _metric_rank(inv, limits, "investment")
    if turn is not None:
        ranks["turnover"] = _metric_rank(turn, limits, "turnover")

    for metric, rank in ranks.items():
        label = CATEGORIES[rank] if rank < len(CATEGORIES) else "above Medium"
        notes.append(f"{metric} alone implies: {label}")

    if len(ranks) < 2:
        missing = [m for m in ("investment", "turnover") if m not in ranks]
        floor = max(ranks.values()) if ranks else None
        min_cat = (CATEGORIES[floor] if floor is not None and floor < len(CATEGORIES) else None)
        notes.append("Missing evidence for: " + ", ".join(missing))
        status = ASSESS_INSUFFICIENT
        if floor is not None and floor >= len(CATEGORIES):
            return ClassificationResult("NOT_MSME", None, None, version["version"], version["source"],
                                        tuple(notes + ["Known metric already exceeds the Medium limit"]))
        return ClassificationResult(status, None, min_cat, version["version"], version["source"], tuple(notes))

    composite = max(ranks.values())
    if composite >= len(CATEGORIES):
        return ClassificationResult("NOT_MSME", None, None, version["version"], version["source"],
                                    tuple(notes + ["Exceeds Medium limit on at least one metric"]))
    return ClassificationResult("CLASSIFIED", CATEGORIES[composite], CATEGORIES[composite],
                                version["version"], version["source"], tuple(notes))


# ---------------------------------------------------------------------------
# GST calendar
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Deadline:
    form: str
    period: str
    due_date: date
    basis: str                # STATUTORY_DEFAULT | EXTENSION_OVERRIDE
    rule_version: str
    source: dict
    optional: bool = False
    note: str = ("Preparation reminder only. This is not a filing. Government extensions may apply; "
                 "confirm on the official GST portal.")


def _add_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def gst_deadlines(
    filer_type: str,
    fy_start_year: int,
    state_category: Optional[str] = None,
    overrides: Iterable[dict] = (),
) -> list[Deadline]:
    """Deadlines for the financial year April ``fy_start_year`` .. March ``fy_start_year+1``.

    ``overrides``: dicts with ``form``, ``period``, ``due_date`` (ISO) and ``source``
    for notified extensions; matching deadlines are replaced and marked
    EXTENSION_OVERRIDE.
    """
    data = _load("gst_calendar")
    fy_start = date(fy_start_year, 4, 1)
    version = select_version(data["versions"], fy_start, "GST calendar")
    if filer_type not in version["filer_types"]:
        raise RuleError(f"Unknown filer_type {filer_type!r}")
    if state_category is not None and state_category not in data["qrmp_state_categories"]:
        raise RuleError(f"state_category must be one of {sorted(data['qrmp_state_categories'])}")

    out: list[Deadline] = []

    def add(form: str, period: str, due: date, optional: bool = False) -> None:
        out.append(Deadline(form, period, due, "STATUTORY_DEFAULT", version["version"], version["source"], optional))

    for ob in version["filer_types"][filer_type]:
        kind = ob["kind"]
        form = ob["form"]
        if kind == "monthly_after_period":
            for i in range(12):
                y, m = _add_month(fy_start_year, 4, i)
                dy, dm = _add_month(y, m, 1)
                add(form, f"{y}-{m:02d}", date(dy, dm, ob["day"]))
        elif kind == "monthly_after_period_months_1_2_of_quarter":
            for q in range(4):
                for k in (0, 1):
                    y, m = _add_month(fy_start_year, 4, q * 3 + k)
                    dy, dm = _add_month(y, m, 1)
                    add(form, f"{y}-{m:02d}", date(dy, dm, ob["day"]), bool(ob.get("optional")))
        elif kind == "quarterly_after_quarter":
            if "state_category_days" in ob:
                if state_category is None:
                    raise RuleError("state_category ('X' or 'Y') is required for QRMP GSTR-3B; it is never inferred")
                day = ob["state_category_days"][state_category]
            else:
                day = ob["day"]
            for q in range(4):
                sy, sm = _add_month(fy_start_year, 4, q * 3)
                ey, em = _add_month(sy, sm, 2)
                dy, dm = _add_month(ey, em, 1)
                add(form, f"{sy}-{sm:02d}..{ey}-{em:02d}", date(dy, dm, day))
        elif kind == "annual":
            year = fy_start_year + 1 + ob["years_after_fy_end"]
            add(form, f"FY{fy_start_year}-{str(fy_start_year + 1)[2:]}", date(year, ob["month"], ob["day"]))
        else:  # pragma: no cover - data error
            raise RuleError(f"Unknown obligation kind {kind!r}")

    by_key = {(o["form"], o["period"]): o for o in overrides}
    final: list[Deadline] = []
    for d in out:
        o = by_key.get((d.form, d.period))
        if o:
            src = {"title": o.get("source", "user-supplied extension notice"), "verification": "USER_SUPPLIED"}
            final.append(Deadline(d.form, d.period, date.fromisoformat(o["due_date"]), "EXTENSION_OVERRIDE",
                                  d.rule_version, src, d.optional, d.note))
        else:
            final.append(d)
    final.sort(key=lambda x: (x.due_date, x.form, x.period))
    return final


# ---------------------------------------------------------------------------
# Scheme eligibility
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    key: str
    value: Any
    source: str   # e.g. "user_declared", "document:<id>#<field>", "derived:msme-classification@<version>"


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    description: str
    status: str                 # MET | NOT_MET | UNKNOWN
    evidence: Optional[Fact]
    reason: str
    fact_key: Optional[str] = None   # the fact this criterion reads (also set for UNKNOWN, so callers can ask for it)


@dataclass(frozen=True)
class SchemeAssessment:
    scheme_id: str
    scheme_name: str
    status: str                 # CRITERIA_NOT_MET | CRITERIA_APPEARS_MET | INSUFFICIENT_EVIDENCE
    criteria: tuple[CriterionResult, ...]
    rule_version: str
    effective_from: str
    source: dict
    notes: tuple[str, ...]
    application_route: str
    unverified_details: tuple[str, ...]
    typical_documents: dict
    lender_decision_required: bool = True
    disclaimer: str = ""


def list_schemes() -> list[dict]:
    return [{"id": s["id"], "name": s["name"]} for s in _load("schemes")["schemes"]]


def facts_from_classification(result: ClassificationResult) -> list[Fact]:
    if result.status != "CLASSIFIED":
        return []
    return [Fact("msme_category", result.category, f"derived:msme-classification@{result.rule_version}")]


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "true":
        return actual is True
    if op == "eq":
        return actual == expected
    if op == "in":
        return actual in expected
    if op in ("lte", "gte", "gt", "lt"):
        a, e = Decimal(str(actual)), Decimal(str(expected))
        return {"lte": a <= e, "gte": a >= e, "gt": a > e, "lt": a < e}[op]
    raise RuleError(f"Unknown operator {op!r}")


def evaluate_scheme(scheme_id: str, facts: Iterable[Fact], as_of: date) -> SchemeAssessment:
    data = _load("schemes")
    scheme = next((s for s in data["schemes"] if s["id"] == scheme_id), None)
    if scheme is None:
        raise RuleError(f"Unknown scheme {scheme_id!r}")
    version = select_version(scheme["versions"], as_of, f"scheme {scheme_id}")
    fact_map = {f.key: f for f in facts}

    results: list[CriterionResult] = []
    for c in version["criteria"]:
        when = c.get("when")
        if when:
            gate = fact_map.get(when["fact"])
            if gate is None:
                results.append(CriterionResult(c["id"], c["description"], CRITERION_UNKNOWN, None,
                                               f"Cannot tell whether this applies: '{when['fact']}' not provided",
                                               when["fact"]))
                continue
            if not _compare(when["op"], gate.value, when.get("value")):
                results.append(CriterionResult(c["id"], c["description"], CRITERION_MET, gate,
                                               "Not applicable on the stated facts", c["fact"]))
                continue
        fact = fact_map.get(c["fact"])
        if fact is None or fact.value is None:
            results.append(CriterionResult(c["id"], c["description"], CRITERION_UNKNOWN, None,
                                           f"No evidence provided for '{c['fact']}'", c["fact"]))
            continue
        ok = _compare(c["op"], fact.value, c.get("value"))
        results.append(CriterionResult(c["id"], c["description"], CRITERION_MET if ok else CRITERION_NOT_MET, fact,
                                       f"{c['fact']}={fact.value!r} vs rule {c['op']} {c.get('value', '')}".strip(),
                                       c["fact"]))

    statuses = {r.status for r in results}
    if CRITERION_NOT_MET in statuses:
        overall = ASSESS_NOT_MET
    elif CRITERION_UNKNOWN in statuses:
        overall = ASSESS_INSUFFICIENT
    else:
        overall = ASSESS_APPEARS_MET

    return SchemeAssessment(
        scheme_id=scheme["id"], scheme_name=scheme["name"], status=overall, criteria=tuple(results),
        rule_version=version["version"], effective_from=version["effective_from"], source=version["source"],
        notes=tuple(version.get("notes", [])), application_route=scheme["application_route"],
        unverified_details=tuple(scheme.get("unverified_details", [])),
        typical_documents=scheme.get("typical_documents", {}), disclaimer=data["disclaimer"],
    )
