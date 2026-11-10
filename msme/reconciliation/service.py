"""Reconciliation service: suggest, review, confirm, reject, unmatch, queues.

  * Bank data is READ-ONLY here. The ERP's own bank reconciliation owns `allocated_amount`/`status`;
    matches live in "MSME Reconciliation Match".
  * Suggestions are heuristic (explained by named reasons and points) until a person confirms them.
  * The database enforces at most one confirmed match per document and per transaction.
  * Every state change is a hash-chained audit event written in the same transaction.
  * A rejected pair is remembered and never re-suggested; confirmed documents/transactions leave the pool.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from lambda_erp.database import get_db
from msme.compliance import audit
from msme.compliance.figures import direction_for, load_approved_documents
from msme.documents.checks import money

from . import matching
from .matching import Doc, Txn

LIVE_STATUSES = ("suggested", "alternative")
_TWO = Decimal("0.01")


class ReconError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def pair_id(company: str, document_id: str, bank_transaction: str) -> str:
    """Stable id for a (company, document, transaction) pair: it survives re-runs and every status change, so a
    reviewer's open screen never points at a match that vanished because someone else re-ran the matcher."""
    digest = hashlib.sha256(f"{company}\x1f{document_id}\x1f{bank_transaction}".encode()).hexdigest()
    return "MRCM-" + digest[:12]


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 2))).quantize(_TWO)
    except (ValueError, TypeError):
        return None


def _date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


class ReconciliationService:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ plumbing
    def _ts(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _today(self) -> date:
        return self._clock().date()

    def _tx(self, fn: Callable[[], Any]) -> Any:
        try:
            return audit.run_in_transaction(get_db(), fn)
        except audit.AuditBusy:
            raise ReconError("busy", "The system is busy; please retry", 503)

    def _audit(self, actor: str, action: str, company: str, detail: dict) -> None:
        audit.append(get_db(), self._ts(), actor, action, "company", company, detail)

    def _profile(self, company: str) -> dict:
        rows = get_db().sql('SELECT * FROM "MSME Compliance Profile" WHERE company = ?', [company])
        if not rows:
            raise ReconError("profile_required", "Set the compliance profile (with the company GSTIN) first", 409)
        if not rows[0]["gstin"]:
            raise ReconError("gstin_required", "The company GSTIN is needed to tell sales from purchases", 409)
        return dict(rows[0])

    # ------------------------------------------------------------------ data
    def _load_documents(self, company: str) -> tuple[list[Doc], dict]:
        gstin = self._profile(company)["gstin"]
        docs, attention = [], {"unclassified": [], "non_inr": [], "incomplete": [], "credit_notes": [], "duplicates": []}
        for d in load_approved_documents(get_db()).values():
            v = d["values"]
            if d["duplicate_of"]:
                attention["duplicates"].append(d["id"]); continue
            if d["type"] == "credit_note":
                attention["credit_notes"].append(d["id"]); continue
            if d["type"] != "invoice":
                continue
            direction = direction_for(v, gstin)
            if direction == "unclassified":
                attention["unclassified"].append(d["id"]); continue
            if (v.get("currency") or "INR").upper() != "INR":
                attention["non_inr"].append(d["id"]); continue
            total, issued = money(v.get("total_amount")), _date(v.get("issue_date"))
            if total is None or issued is None or not (v.get("invoice_number") or "").strip():
                attention["incomplete"].append(d["id"]); continue
            party = v.get("buyer.name" if direction == "outward" else "seller.name") or ""
            docs.append(Doc(d["id"], direction, v["invoice_number"], issued, _date(v.get("due_date")), total, party))
        return sorted(docs, key=lambda x: x.id), attention

    def _load_transactions(self, company: str) -> tuple[list[Txn], dict]:
        rows = get_db().sql(
            'SELECT t.name, t.posting_date, t.deposit, t.withdrawal, t.currency, t.description, t.remittance_information, '
            't.reference_number, t.counterparty_name FROM "Bank Transaction" t '
            'JOIN "Bank Account" a ON t.bank_account_id = a.name WHERE a.company = ? AND t.docstatus <> 2 '
            "AND COALESCE(t.reversal_indicator, 0) = 0 AND COALESCE(t.status, '') <> 'Informational' ORDER BY t.name",
            [company])
        txns, attention = [], {"non_inr": [], "unreadable": []}
        for r in rows:
            if (r["currency"] or "INR").upper() != "INR":
                attention["non_inr"].append(r["name"]); continue
            posted, dep, wd = _date(r["posting_date"]), _dec(r["deposit"]) or Decimal(0), _dec(r["withdrawal"]) or Decimal(0)
            if posted is None or (dep <= 0 and wd <= 0):
                attention["unreadable"].append(r["name"]); continue
            kind, amount = ("deposit", dep) if dep > 0 else ("withdrawal", wd)
            text = " ".join(str(x) for x in (r["description"], r["remittance_information"], r["reference_number"]) if x)
            txns.append(Txn(r["name"], posted, amount, kind, text, r["counterparty_name"] or ""))
        return txns, attention

    # ------------------------------------------------------------------ run
    def run(self, company: str, user: str) -> dict:
        """Regenerate suggestions for everything not yet confirmed. Deterministic and idempotent."""
        docs, doc_attention = self._load_documents(company)
        txns, txn_attention = self._load_transactions(company)
        db = get_db()
        confirmed = db.sql('SELECT document_id, bank_transaction FROM "MSME Reconciliation Match" WHERE company = ? '
                           "AND status = 'confirmed'", [company])
        rejected = {(r["document_id"], r["bank_transaction"]) for r in db.sql(
            'SELECT document_id, bank_transaction FROM "MSME Reconciliation Match" WHERE company = ? AND status = \'rejected\'',
            [company])}
        done_docs, done_txns = {r["document_id"] for r in confirmed}, {r["bank_transaction"] for r in confirmed}
        pool_docs = [d for d in docs if d.id not in done_docs]
        pool_txns = [t for t in txns if t.id not in done_txns]
        candidates = [c for c in matching.generate_candidates(pool_docs, pool_txns) if (c.doc_id, c.txn_id) not in rejected]
        primary, alternatives = matching.assign(candidates)
        stamp, counts = self._ts(), {}

        def work():
            db.sql('DELETE FROM "MSME Reconciliation Match" WHERE company = ? AND status IN (\'suggested\', \'alternative\', '
                   "'superseded')", [company])
            for status, items in (("suggested", primary), ("alternative", alternatives)):
                for c in items:
                    db.insert("MSME Reconciliation Match", {
                        "id": pair_id(company, c.doc_id, c.txn_id), "company": company, "document_id": c.doc_id,
                        "bank_transaction": c.txn_id, "status": status, "classification": c.classification, "score": c.score,
                        "basis_json": json.dumps({"reasons": list(c.reasons), "ambiguous": c.ambiguous,
                                                  "txn_also_suggested_for": list(c.txn_also_suggested_for)}),
                        "difference": str(c.difference), "algorithm_version": matching.ALGORITHM_VERSION,
                        "created_by": user, "created_at": stamp})
            counts.update(suggested=len(primary), alternatives=len(alternatives),
                          strong=sum(1 for c in primary if c.classification == "STRONG"),
                          probable=sum(1 for c in primary if c.classification == "PROBABLE"),
                          possible=sum(1 for c in primary if c.classification == "POSSIBLE"),
                          ambiguous=sum(1 for c in primary if c.ambiguous))
            self._audit(user, "recon.run", company, {"algorithm": matching.ALGORITHM_VERSION, "documents": len(pool_docs),
                                                      "transactions": len(pool_txns), **counts})
        self._tx(work)
        return {"company": company, "algorithm_version": matching.ALGORITHM_VERSION, "documents_considered": len(pool_docs),
                "transactions_considered": len(pool_txns), **counts,
                "document_attention": {k: v for k, v in doc_attention.items() if v},
                "transaction_attention": {k: v for k, v in txn_attention.items() if v},
                "note": "Suggestions are heuristic until a person confirms them. One invoice to one payment only; split or "
                        "aggregate payments are not matched."}

    # ------------------------------------------------------------------ review
    def _match(self, match_id: str) -> dict:
        rows = get_db().sql('SELECT * FROM "MSME Reconciliation Match" WHERE id = ?', [match_id])
        if not rows:
            raise ReconError("not_found", "Match not found", 404)
        return dict(rows[0])

    def confirm(self, match_id: str, user: str, note: Optional[str] = None) -> dict:
        m = self._match(match_id)
        if m["status"] not in LIVE_STATUSES:
            raise ReconError("not_confirmable", f"Match is '{m['status']}'", 409)
        if note and len(note) > 300:
            raise ReconError("note_too_long", "Notes are limited to 300 characters", 422)
        stamp, db = self._ts(), get_db()

        def work():
            self._assert_free(m["document_id"], m["bank_transaction"], m["id"])
            done = db.sql('UPDATE "MSME Reconciliation Match" SET status = \'confirmed\', decided_by = ?, decided_at = ?, '
                          "note = ? WHERE id = ? AND status IN ('suggested', 'alternative') RETURNING id",
                          [user, stamp, note, match_id])
            if not done:
                raise ReconError("not_confirmable", "Match changed state concurrently", 409)
            db.sql('UPDATE "MSME Reconciliation Match" SET status = \'superseded\' WHERE id <> ? AND status IN '
                   "('suggested', 'alternative') AND (document_id = ? OR bank_transaction = ?)",
                   [match_id, m["document_id"], m["bank_transaction"]])
            self._audit(user, "recon.confirm", m["company"], {
                "match_id": match_id, "document_id": m["document_id"], "bank_transaction": m["bank_transaction"],
                "classification": m["classification"], "score": m["score"], "difference": m["difference"]})
        self._tx(work)
        return self.get_match(match_id)

    def _assert_free(self, document_id: str, txn: str, except_id: Optional[str]) -> None:
        db = get_db()
        for column, value, label in (("document_id", document_id, "document"), ("bank_transaction", txn, "bank transaction")):
            rows = db.sql(f'SELECT id FROM "MSME Reconciliation Match" WHERE {column} = ? AND status = \'confirmed\'', [value])
            if rows and rows[0]["id"] != except_id:
                raise ReconError("already_matched", f"This {label} already has a confirmed match", 409)

    def reject(self, match_id: str, user: str, reason: str) -> dict:
        reason = (reason or "").strip()
        if not reason or len(reason) > 300:
            raise ReconError("reason_required", "A reason of 1-300 characters is required", 422)
        m = self._match(match_id)
        if m["status"] not in LIVE_STATUSES:
            raise ReconError("not_rejectable", f"Match is '{m['status']}'", 409)
        stamp, db = self._ts(), get_db()

        def work():
            done = db.sql('UPDATE "MSME Reconciliation Match" SET status = \'rejected\', decided_by = ?, decided_at = ?, '
                          "note = ? WHERE id = ? AND status IN ('suggested', 'alternative') RETURNING id", [user, stamp, reason, match_id])
            if not done:
                raise ReconError("not_rejectable", "Match changed state concurrently", 409)
            self._audit(user, "recon.reject", m["company"], {"match_id": match_id, "document_id": m["document_id"],
                                                             "bank_transaction": m["bank_transaction"], "reason": reason})
        self._tx(work)
        return self.get_match(match_id)

    def unmatch(self, match_id: str, user: str, reason: str) -> dict:
        reason = (reason or "").strip()
        if not reason or len(reason) > 300:
            raise ReconError("reason_required", "A reason of 1-300 characters is required", 422)
        m = self._match(match_id)
        if m["status"] != "confirmed":
            raise ReconError("not_confirmed", f"Match is '{m['status']}'", 409)
        stamp, db = self._ts(), get_db()

        def work():
            done = db.sql('UPDATE "MSME Reconciliation Match" SET status = \'rejected\', decided_by = ?, decided_at = ?, '
                          "note = ? WHERE id = ? AND status = 'confirmed' RETURNING id",
                          [user, stamp, f"Unmatched after confirmation: {reason}", match_id])
            if not done:
                raise ReconError("not_confirmed", "Match changed state concurrently", 409)
            self._audit(user, "recon.unmatch", m["company"], {"match_id": match_id, "document_id": m["document_id"],
                                                              "bank_transaction": m["bank_transaction"], "reason": reason})
        self._tx(work)
        return self.get_match(match_id)

    def manual_match(self, company: str, document_id: str, bank_transaction: str, user: str, note: str) -> dict:
        note = (note or "").strip()
        if not note or len(note) > 300:
            raise ReconError("reason_required", "A note of 1-300 characters is required", 422)
        docs, _ = self._load_documents(company)
        doc = next((d for d in docs if d.id == document_id), None)
        if doc is None:
            raise ReconError("document_not_eligible", "The document must be an approved, non-duplicate INR invoice that "
                             "belongs to this company", 422)
        txns, _ = self._load_transactions(company)
        txn = next((t for t in txns if t.id == bank_transaction), None)
        if txn is None:
            raise ReconError("transaction_not_eligible", "The bank transaction must be booked, non-reversed, INR and belong "
                             "to this company's bank account", 422)
        if (doc.direction, txn.kind) not in (("outward", "deposit"), ("inward", "withdrawal")):
            raise ReconError("direction_mismatch", f"A {doc.direction} invoice cannot be settled by a {txn.kind}", 422)
        stamp, db = self._ts(), get_db()
        match_id = pair_id(company, document_id, bank_transaction)

        def work():
            self._assert_free(document_id, bank_transaction, None)
            existing = db.sql('SELECT id FROM "MSME Reconciliation Match" WHERE document_id = ? AND bank_transaction = ?',
                              [document_id, bank_transaction])
            basis = json.dumps({"manual": True, "reasons": [], "ambiguous": False, "txn_also_suggested_for": []})
            diff = str(txn.amount - doc.total)
            if existing:
                db.sql('UPDATE "MSME Reconciliation Match" SET status = \'confirmed\', classification = \'MANUAL\', '
                       "score = NULL, basis_json = ?, difference = ?, decided_by = ?, decided_at = ?, note = ? WHERE id = ?",
                       [basis, diff, user, stamp, note, existing[0]["id"]])
                mid = existing[0]["id"]
            else:
                mid = match_id
                db.insert("MSME Reconciliation Match", {
                    "id": mid, "company": company, "document_id": document_id, "bank_transaction": bank_transaction,
                    "status": "confirmed", "classification": "MANUAL", "score": None, "basis_json": basis, "difference": diff,
                    "algorithm_version": matching.ALGORITHM_VERSION, "created_by": user, "created_at": stamp,
                    "decided_by": user, "decided_at": stamp, "note": note})
            db.sql('UPDATE "MSME Reconciliation Match" SET status = \'superseded\' WHERE id <> ? AND status IN '
                   "('suggested', 'alternative') AND (document_id = ? OR bank_transaction = ?)", [mid, document_id, bank_transaction])
            self._audit(user, "recon.manual", company, {"match_id": mid, "document_id": document_id,
                                                        "bank_transaction": bank_transaction, "difference": diff, "note": note})
            return mid
        return self.get_match(self._tx(work))

    # ------------------------------------------------------------------ reads
    def _enrich(self, rows: list[dict]) -> list[dict]:
        db = get_db()
        out = []
        for m in rows:
            basis = json.loads(m["basis_json"])
            doc = {r["field_path"]: r["current_value"] for r in db.sql(
                'SELECT field_path, current_value FROM "MSME Extracted Field" WHERE document_id = ? AND field_path IN '
                "('invoice_number', 'issue_date', 'total_amount', 'buyer.name', 'seller.name')", [m["document_id"]])}
            t = db.sql('SELECT posting_date, deposit, withdrawal, description, counterparty_name FROM "Bank Transaction" '
                       "WHERE name = ?", [m["bank_transaction"]])
            t = dict(t[0]) if t else {}
            out.append({
                "id": m["id"], "status": m["status"], "classification": m["classification"], "score": m["score"],
                "difference": m["difference"], "reasons": basis.get("reasons", []), "ambiguous": basis.get("ambiguous", False),
                "manual": basis.get("manual", False), "txn_also_suggested_for": basis.get("txn_also_suggested_for", []),
                "document": {"id": m["document_id"], "invoice_number": doc.get("invoice_number"), "issue_date": doc.get("issue_date"),
                             "total": doc.get("total_amount"), "counterparty": doc.get("buyer.name") or doc.get("seller.name")},
                "bank_transaction": {"name": m["bank_transaction"], "posting_date": t.get("posting_date"),
                                     "amount": t.get("deposit") or t.get("withdrawal"), "kind": "deposit" if (t.get("deposit") or 0) > 0 else "withdrawal",
                                     "description": t.get("description"), "counterparty": t.get("counterparty_name")},
                "decided_by": m["decided_by"], "decided_at": m["decided_at"], "note": m["note"],
                "algorithm_version": m["algorithm_version"]})
        return out

    def get_match(self, match_id: str) -> dict:
        return self._enrich([self._match(match_id)])[0]

    def list_matches(self, company: str, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        where, params = "WHERE company = ?", [company]
        if status:
            where += " AND status = ?"
            params.append(status)
        db = get_db()
        total = db.sql(f'SELECT COUNT(*) AS n FROM "MSME Reconciliation Match" {where}', params)[0]["n"]
        rows = db.sql(f'SELECT * FROM "MSME Reconciliation Match" {where} ORDER BY score DESC, id LIMIT ? OFFSET ?', [*params, limit, offset])
        return {"total": total, "rows": self._enrich([dict(r) for r in rows])}

    def unmatched(self, company: str, kind: str, limit: int = 50, offset: int = 0) -> dict:
        """Records with no CONFIRMED match, oldest first, with age in days."""
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        db, today = get_db(), self._today()
        confirmed = db.sql('SELECT document_id, bank_transaction FROM "MSME Reconciliation Match" WHERE company = ? '
                           "AND status = 'confirmed'", [company])
        if kind == "documents":
            docs, _ = self._load_documents(company)
            done = {r["document_id"] for r in confirmed}
            rows = [{"id": d.id, "direction": d.direction, "invoice_number": d.invoice_number, "issue_date": d.issue_date.isoformat(),
                     "total": str(d.total), "counterparty": d.counterparty, "age_days": (today - d.issue_date).days}
                    for d in sorted(docs, key=lambda x: (x.issue_date, x.id)) if d.id not in done]
        elif kind == "transactions":
            txns, _ = self._load_transactions(company)
            done = {r["bank_transaction"] for r in confirmed}
            rows = [{"name": t.id, "kind": t.kind, "posting_date": t.posting_date.isoformat(), "amount": str(t.amount),
                     "description": t.text[:200], "counterparty": t.counterparty, "age_days": (today - t.posting_date).days}
                    for t in sorted(txns, key=lambda x: (x.posting_date, x.id)) if t.id not in done]
        else:
            raise ReconError("invalid_kind", "kind must be 'documents' or 'transactions'", 422)
        return {"kind": kind, "total": len(rows), "rows": rows[offset: offset + limit]}

    def summary(self, company: str) -> dict:
        docs, doc_attention = self._load_documents(company)
        txns, txn_attention = self._load_transactions(company)
        db = get_db()
        confirmed = db.sql('SELECT document_id, bank_transaction FROM "MSME Reconciliation Match" WHERE company = ? '
                           "AND status = 'confirmed'", [company])
        md, mt = {r["document_id"] for r in confirmed}, {r["bank_transaction"] for r in confirmed}
        by_status = {r["status"]: r["n"] for r in db.sql('SELECT status, COUNT(*) AS n FROM "MSME Reconciliation Match" '
                                                         "WHERE company = ? GROUP BY status", [company])}

        def block(items, matched, key):
            m = [x for x in items if x.id in matched]
            u = [x for x in items if x.id not in matched]
            amount = lambda xs: str(sum((getattr(x, key) for x in xs), Decimal(0)).quantize(_TWO))  # noqa: E731
            return {"matched": len(m), "matched_value": amount(m), "unmatched": len(u), "unmatched_value": amount(u)}

        return {"company": company,
                "invoices": {"outward": block([d for d in docs if d.direction == "outward"], md, "total"),
                             "inward": block([d for d in docs if d.direction == "inward"], md, "total")},
                "bank": {"deposits": block([t for t in txns if t.kind == "deposit"], mt, "amount"),
                         "withdrawals": block([t for t in txns if t.kind == "withdrawal"], mt, "amount")},
                "matches_by_status": by_status,
                "document_attention": {k: v for k, v in doc_attention.items() if v},
                "transaction_attention": {k: v for k, v in txn_attention.items() if v}}
