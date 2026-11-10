"""Deterministic, explainable matching of approved invoices to bank transactions (pure functions, no I/O).

Every suggested pair carries the reasons and points that produced its score. Nothing here is a decision:
a person confirms or rejects. Known limits (documented, not hidden): one invoice to one transaction only (no
split or aggregate payments), INR only, and a shortfall is flagged as a *possible* deduction at source without
assuming any rate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

ALGORITHM_VERSION = "match-v1"
AMOUNT_TOLERANCE = Decimal("0.01")
MAX_SHORTFALL = Decimal("0.10")          # up to 10% short is flagged as a possible deduction at source
DAYS_BEFORE_INVOICE = 30                 # advances
DAYS_AFTER_INVOICE = 180
POINTS = {"AMOUNT_EXACT": 50, "AMOUNT_SHORTFALL": 20, "REFERENCE": 35, "NAME": 15, "TIMING": 5}
STRONG, PROBABLE, POSSIBLE = 85, 60, 40
_STOPWORDS = {"pvt", "private", "ltd", "limited", "llp", "and", "the", "co", "company", "inc", "corp", "of", "m", "s"}


@dataclass(frozen=True)
class Doc:
    id: str
    direction: str                 # outward | inward
    invoice_number: str
    issue_date: date
    due_date: Optional[date]
    total: Decimal
    counterparty: str


@dataclass(frozen=True)
class Txn:
    id: str
    posting_date: date
    amount: Decimal
    kind: str                      # deposit | withdrawal
    text: str                      # description + remittance + reference, as one string
    counterparty: str


@dataclass(frozen=True)
class Candidate:
    doc_id: str
    txn_id: str
    score: int
    classification: str            # STRONG | PROBABLE | POSSIBLE
    reasons: tuple[dict, ...]
    difference: Decimal            # transaction amount minus invoice total
    ambiguous: bool = False
    txn_also_suggested_for: tuple[str, ...] = field(default=())


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in _STOPWORDS and len(t) > 1}


def name_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def reference_in_text(invoice_number: str, text: str) -> bool:
    """The invoice number appears in the transaction text. Short or purely numeric numbers are too ambiguous."""
    needle = _alnum(invoice_number)
    if len(needle) < 4 or (needle.isdigit() and len(needle) < 5):
        return False
    return needle in _alnum(text)


def classify(score: int) -> Optional[str]:
    if score >= STRONG:
        return "STRONG"
    if score >= PROBABLE:
        return "PROBABLE"
    if score >= POSSIBLE:
        return "POSSIBLE"
    return None


def score_pair(doc: Doc, txn: Txn) -> Optional[Candidate]:
    if doc.total <= 0 or txn.amount <= 0:
        return None
    if (doc.direction, txn.kind) not in (("outward", "deposit"), ("inward", "withdrawal")):
        return None
    if not (doc.issue_date - timedelta(days=DAYS_BEFORE_INVOICE) <= txn.posting_date
            <= doc.issue_date + timedelta(days=DAYS_AFTER_INVOICE)):
        return None

    reasons: list[dict] = []
    diff = txn.amount - doc.total

    if abs(diff) <= AMOUNT_TOLERANCE:
        reasons.append({"code": "AMOUNT_EXACT", "points": POINTS["AMOUNT_EXACT"],
                        "detail": f"Amount {txn.amount:.2f} equals the invoice total {doc.total:.2f}"})
    elif diff < 0 and (-diff) / doc.total <= MAX_SHORTFALL:
        pct = (-diff) / doc.total * 100
        reasons.append({"code": "AMOUNT_SHORTFALL", "points": POINTS["AMOUNT_SHORTFALL"],
                        "detail": f"Paid {txn.amount:.2f}, {(-diff):.2f} ({pct:.1f}%) less than {doc.total:.2f}; a deduction "
                                  "at source (e.g. TDS) or a part payment is possible - verify, no rate is assumed"})
    elif diff > 0:
        reasons.append({"code": "AMOUNT_EXCEEDS", "points": 0,
                        "detail": f"Amount {txn.amount:.2f} exceeds the invoice total {doc.total:.2f}"})
    else:
        reasons.append({"code": "AMOUNT_DIFFERS", "points": 0,
                        "detail": f"Amount {txn.amount:.2f} is more than 10% below the invoice total {doc.total:.2f}"})

    if reference_in_text(doc.invoice_number, txn.text):
        reasons.append({"code": "REFERENCE", "points": POINTS["REFERENCE"],
                        "detail": f"Invoice number {doc.invoice_number} appears in the bank narration"})

    sim = max(name_similarity(doc.counterparty, txn.counterparty), name_similarity(doc.counterparty, txn.text))
    if sim >= 0.5:
        reasons.append({"code": "NAME", "points": round(POINTS["NAME"] * sim),
                        "detail": f"Counterparty names are similar ({sim:.0%}): '{doc.counterparty}' vs "
                                  f"'{txn.counterparty or 'narration'}'"})

    horizon = doc.due_date or (doc.issue_date + timedelta(days=60))
    if doc.issue_date <= txn.posting_date <= horizon + timedelta(days=15):
        reasons.append({"code": "TIMING", "points": POINTS["TIMING"],
                        "detail": f"Payment on {txn.posting_date.isoformat()} falls between issue and due date (+15 days)"})

    score = min(100, sum(r["points"] for r in reasons))
    cls = classify(score)
    if cls is None:
        return None
    return Candidate(doc.id, txn.id, score, cls, tuple(reasons), diff)


def generate_candidates(docs: list[Doc], txns: list[Txn]) -> list[Candidate]:
    out = []
    for doc in docs:
        for txn in txns:
            c = score_pair(doc, txn)
            if c:
                out.append(c)
    return out


def assign(candidates: list[Candidate], alternatives_per_doc: int = 2) -> tuple[list[Candidate], list[Candidate]]:
    """Greedy one-to-one assignment by descending score (ties broken by ids, so runs are reproducible).

    Returns (primary suggestions, alternatives). A pair is flagged ``ambiguous`` when another candidate for the same
    invoice, or another invoice for the same transaction, scores identically: the engine cannot tell them apart and a
    person must choose.
    """
    ordered = sorted(candidates, key=lambda c: (-c.score, c.doc_id, c.txn_id))
    used_docs: set[str] = set()
    used_txns: set[str] = set()
    primary: list[Candidate] = []
    for c in ordered:
        if c.doc_id not in used_docs and c.txn_id not in used_txns:
            used_docs.add(c.doc_id)
            used_txns.add(c.txn_id)
            primary.append(c)

    by_doc: dict[str, list[Candidate]] = {}
    by_txn: dict[str, list[Candidate]] = {}
    for c in ordered:
        by_doc.setdefault(c.doc_id, []).append(c)
        by_txn.setdefault(c.txn_id, []).append(c)

    def flagged(c: Candidate, is_primary: bool) -> Candidate:
        same_doc = [o for o in by_doc[c.doc_id] if o is not c and o.score == c.score]
        same_txn = [o for o in by_txn[c.txn_id] if o is not c and o.score == c.score]
        others = tuple(sorted({o.doc_id for o in by_txn[c.txn_id] if o.doc_id != c.doc_id and o.doc_id in used_docs
                               and not is_primary}))
        return Candidate(c.doc_id, c.txn_id, c.score, c.classification, c.reasons, c.difference,
                         bool(same_doc or same_txn), others)

    primaries = [flagged(c, True) for c in primary]
    chosen = {(c.doc_id, c.txn_id) for c in primary}
    alts: list[Candidate] = []
    for doc_id, items in by_doc.items():
        extra = [c for c in items if (c.doc_id, c.txn_id) not in chosen][:alternatives_per_doc]
        alts.extend(flagged(c, False) for c in extra)
    return primaries, alts
