"""Credit-readiness and scheme-navigation service.

  * Facts are append-only and each carries its source; the newest row per key is current.
  * `msme_category` is always derived (effective-dated rules) and never typed in.
  * Scheme assessments are CRITERIA comparisons: NOT_MET / APPEARS_MET / INSUFFICIENT_EVIDENCE. They never say
    "eligible" or "approved"; a lender decision is always required. Every result names the evidence it used,
    the rule version and the official source with its verification status.
  * Each persisted assessment is an immutable snapshot (rule version + digest of the facts used) and is audited
    in the same hash chain as the compliance workspace.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from lambda_erp.database import get_db
from msme.compliance import audit
from msme.rules import Fact, RuleError, classify_msme, evaluate_scheme, list_schemes
from msme.rules.engine import _load as _load_rules

from .turnover import turnover_check as _turnover_check

from . import facts as fact_registry
from .readiness import ATTESTABLE, build_readiness

_counter = itertools.count(1)
_counter_lock = threading.Lock()


class CreditError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


class CreditService:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ plumbing
    def _ts(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _today(self) -> date:
        return self._clock().date()

    def _row_id(self, prefix: str) -> str:
        """Time-sortable id: newest row per key is the one with the greatest id (stable even within a second)."""
        with _counter_lock:
            n = next(_counter)
        return f"{prefix}-{int(self._clock().timestamp() * 1_000_000):016d}{n % 1_000_000:06d}"

    def _tx(self, fn: Callable[[], Any]) -> Any:
        try:
            return audit.run_in_transaction(get_db(), fn)
        except audit.AuditBusy:
            raise CreditError("busy", "The system is busy; please retry", 503)

    def _audit(self, actor: str, action: str, entity_type: str, entity_id: str, detail: dict) -> None:
        audit.append(get_db(), self._ts(), actor, action, entity_type, entity_id, detail)

    def _require_company(self, company: str) -> None:
        if not get_db().sql('SELECT name FROM "Company" WHERE name = ?', [company]):
            raise CreditError("company_not_found", "Unknown company", 404)

    # ------------------------------------------------------------------ facts
    def _current(self, company: str, kind: str = "fact") -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in get_db().sql('SELECT * FROM "MSME Credit Fact" WHERE company = ? AND kind = ? ORDER BY id', [company, kind]):
            out[r["fact_key"]] = dict(r)          # ascending id: the last assignment is the newest
        return out

    def set_fact(self, company: str, key: str, value: Any, user: str, evidence_document_id: Optional[str] = None) -> dict:
        self._require_company(company)
        try:
            canonical = fact_registry.validate(key, value)
        except fact_registry.FactError as exc:
            raise CreditError(exc.code, exc.message, 422)
        db = get_db()
        if evidence_document_id and not db.sql('SELECT id FROM "MSME Document" WHERE id = ?', [evidence_document_id]):
            raise CreditError("unknown_document", f"Unknown document {evidence_document_id}", 422)
        current = self._current(company).get(key)
        if current and json.loads(current["value_json"]) == canonical and \
                (current["evidence_document_id"] or None) == (evidence_document_id or None):
            return {**self._public_fact(current), "unchanged": True}
        row = {"id": self._row_id("MCFT"), "company": company, "kind": "fact", "fact_key": key,
               "value_json": json.dumps(canonical), "source": "user_declared",
               "evidence_document_id": evidence_document_id, "entered_by": user, "entered_at": self._ts()}

        def work():
            db.insert("MSME Credit Fact", row)
            self._audit(user, "credit.fact_set", "company", company, {
                "key": key, "value": canonical, "previous": json.loads(current["value_json"]) if current else None,
                "evidence_document_id": evidence_document_id})
        self._tx(work)
        return {**self._public_fact(row), "unchanged": False}

    @staticmethod
    def _public_fact(row: dict) -> dict:
        return {"key": row["fact_key"], "value": json.loads(row["value_json"]), "source": row["source"],
                "evidence_document_id": row["evidence_document_id"], "entered_by": row["entered_by"],
                "entered_at": row["entered_at"], "description": fact_registry.describe(row["fact_key"])}

    def get_facts(self, company: str) -> dict:
        self._require_company(company)
        current = self._current(company)
        derived, classification = self._derive(current, self._today())
        return {"company": company, "facts": [self._public_fact(r) for r in current.values()],
                "derived": [{"key": f.key, "value": f.value, "source": f.source} for f in derived],
                "classification": classification,
                "settable": {k: {"type": s.type, "description": s.description, "choices": list(s.choices)}
                             for k, s in fact_registry.SPECS.items()}}

    def fact_history(self, company: str, key: str) -> list[dict]:
        self._require_company(company)
        rows = get_db().sql('SELECT * FROM "MSME Credit Fact" WHERE company = ? AND kind = \'fact\' AND fact_key = ? '
                            "ORDER BY id", [company, key])
        return [self._public_fact(dict(r)) for r in rows]

    def _derive(self, current: dict[str, dict], as_of: date) -> tuple[list[Fact], dict]:
        value = lambda k: json.loads(current[k]["value_json"]) if k in current else None  # noqa: E731
        result = classify_msme(value("investment_inr"), value("turnover_inr"), as_of)
        classification = {"status": result.status, "category": result.category, "minimum_category": result.minimum_category,
                          "rule_version": result.rule_version, "source": result.source,
                          "explanation": list(result.explanation)}
        derived: list[Fact] = []
        source = f"derived:msme-classification@{result.rule_version}"
        if result.status == "CLASSIFIED":
            derived.append(Fact("msme_category", result.category, source))
        elif result.status == "NOT_MSME":
            derived.append(Fact("msme_category", "NOT_MSME", source))
        return derived, classification

    def _fact_objects(self, current: dict[str, dict]) -> list[Fact]:
        out = []
        for key, row in current.items():
            src = "user_declared" + (f"+document:{row['evidence_document_id']}" if row["evidence_document_id"] else "")
            out.append(Fact(key, json.loads(row["value_json"]), src))
        return out

    # ------------------------------------------------------------------ readiness
    def attest(self, company: str, item_key: str, status: str, note: str, document_id: Optional[str], user: str) -> dict:
        self._require_company(company)
        if item_key not in ATTESTABLE:
            raise CreditError("not_attestable", "This item is evidenced by platform data and cannot be self-attested", 422)
        if status not in ("provided", "not_applicable"):
            raise CreditError("invalid_status", "status must be 'provided' or 'not_applicable'", 422)
        note = (note or "").strip()
        if not note or len(note) > 300:
            raise CreditError("note_required", "A note of 1-300 characters is required", 422)
        db = get_db()
        if document_id and not db.sql('SELECT id FROM "MSME Document" WHERE id = ?', [document_id]):
            raise CreditError("unknown_document", f"Unknown document {document_id}", 422)
        row = {"id": self._row_id("MCFT"), "company": company, "kind": "attestation", "fact_key": item_key,
               "value_json": json.dumps({"status": status, "note": note}), "source": "user_attested",
               "evidence_document_id": document_id, "entered_by": user, "entered_at": self._ts()}

        def work():
            db.insert("MSME Credit Fact", row)
            self._audit(user, "credit.attest", "company", company,
                        {"item": item_key, "status": status, "document_id": document_id, "verified": False})
        self._tx(work)
        return {"item": item_key, "status": status, "note": note, "document_id": document_id, "verified": False}

    def readiness(self, company: str) -> dict:
        self._require_company(company)
        att = {}
        for k, r in self._current(company, "attestation").items():
            v = json.loads(r["value_json"])
            att[k] = {"status": v["status"], "note": v["note"], "document_id": r["evidence_document_id"],
                      "entered_by": r["entered_by"], "entered_at": r["entered_at"]}
        return build_readiness(get_db(), company, self._today(), self._current(company), att)

    def turnover_check(self, company: str) -> dict:
        """Heuristic cross-check of the declared turnover against approved invoices and bank credits."""
        self._require_company(company)
        row = self._current(company).get("turnover_inr")
        declared = Decimal(str(json.loads(row["value_json"]))) if row else None
        result = _turnover_check(get_db(), company, self._today(), declared)
        if row:
            result["declared_by"], result["declared_at"], result["declared_source"] = row["entered_by"], row["entered_at"], row["source"]
        return result

    # ------------------------------------------------------------------ schemes
    @staticmethod
    def catalogue(as_of: date) -> list[dict]:
        out = []
        for s in _load_rules("schemes")["schemes"]:
            try:
                from msme.rules.engine import select_version
                v = select_version(s["versions"], as_of, s["id"])
                out.append({"id": s["id"], "name": s["name"], "administrator": s["administrator"],
                            "application_route": s["application_route"], "rule_version": v["version"],
                            "effective_from": v["effective_from"], "source": v["source"],
                            "unverified_details": s.get("unverified_details", [])})
            except RuleError as exc:
                out.append({"id": s["id"], "name": s["name"], "available": False, "message": str(exc)})
        return out

    def _one_assessment(self, scheme_id: str, facts: list[Fact], as_of: date, classification: dict) -> dict:
        try:
            a = evaluate_scheme(scheme_id, facts, as_of)
        except RuleError as exc:
            return {"scheme_id": scheme_id, "status": "NOT_AVAILABLE_ON_DATE", "message": str(exc)}
        criteria, missing = [], {}
        for c in a.criteria:
            criteria.append({"id": c.criterion_id, "description": c.description, "status": c.status, "reason": c.reason,
                             "fact_key": c.fact_key, "evidence_source": c.evidence.source if c.evidence else None})
            if c.status == "UNKNOWN" and c.fact_key:
                if c.fact_key == "msme_category":
                    for k in ("investment_inr", "turnover_inr"):
                        missing[k] = fact_registry.describe(k)
                else:
                    missing[c.fact_key] = fact_registry.describe(c.fact_key)
        if classification["status"] == "INSUFFICIENT_EVIDENCE" and any(c["fact_key"] == "msme_category" for c in criteria):
            for k in ("investment_inr", "turnover_inr"):
                missing.setdefault(k, fact_registry.describe(k))
        return {"scheme_id": a.scheme_id, "scheme_name": a.scheme_name, "status": a.status, "criteria": criteria,
                "missing_facts": [{"key": k, "description": d} for k, d in missing.items()],
                "rule_version": a.rule_version, "effective_from": a.effective_from, "source": a.source,
                "notes": list(a.notes), "application_route": a.application_route,
                "unverified_details": list(a.unverified_details), "typical_documents": a.typical_documents,
                "lender_decision_required": True, "disclaimer": a.disclaimer}

    def _evaluate_all(self, company: str, as_of: date) -> tuple[list[dict], dict, str]:
        current = self._current(company)
        derived, classification = self._derive(current, as_of)
        facts = self._fact_objects(current) + derived
        digest = hashlib.sha256(json.dumps(
            {"as_of": as_of.isoformat(), "facts": sorted((f.key, f.value, f.source) for f in facts)},
            sort_keys=True, default=str).encode()).hexdigest()
        results = [self._one_assessment(s["id"], facts, as_of, classification) for s in list_schemes()]
        return results, classification, digest

    def assess(self, company: str, user: str) -> dict:
        """Evaluate every scheme and persist an immutable snapshot of each result."""
        self._require_company(company)
        as_of = self._today()
        results, classification, digest = self._evaluate_all(company, as_of)
        db, stamp, ids = get_db(), self._ts(), {}

        def work():
            for r in results:
                if r["status"] == "NOT_AVAILABLE_ON_DATE":
                    continue
                sid = self._row_id("MSCA")
                ids[r["scheme_id"]] = sid
                db.insert("MSME Scheme Assessment", {
                    "id": sid, "company": company, "scheme_id": r["scheme_id"], "as_of": as_of.isoformat(),
                    "rule_version": r["rule_version"], "status": r["status"], "result_json": json.dumps(r),
                    "facts_digest": digest, "created_by": user, "created_at": stamp})
            self._audit(user, "scheme.assess", "company", company, {
                "as_of": as_of.isoformat(), "facts_digest": digest, "snapshots": ids,
                "results": {r["scheme_id"]: r["status"] for r in results}})
        self._tx(work)
        return {"company": company, "as_of": as_of.isoformat(), "classification": classification,
                "facts_digest": digest, "snapshot_ids": ids, "assessments": results,
                "disclaimer": _load_rules("schemes")["disclaimer"]}

    def assessments(self, company: str, scheme_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        self._require_company(company)
        query, params = 'SELECT * FROM "MSME Scheme Assessment" WHERE company = ?', [company]
        if scheme_id:
            query += " AND scheme_id = ?"
            params.append(scheme_id)
        rows = get_db().sql(query + " ORDER BY id DESC LIMIT ?", [*params, max(1, min(int(limit), 200))])
        return [{"id": r["id"], "scheme_id": r["scheme_id"], "as_of": r["as_of"], "rule_version": r["rule_version"],
                 "status": r["status"], "facts_digest": r["facts_digest"], "created_by": r["created_by"],
                 "created_at": r["created_at"], "result": json.loads(r["result_json"])} for r in rows]

    def application_summary(self, company: str, scheme_id: str) -> dict:
        """Live (not persisted) preparation summary for one scheme: facts used, criteria, gaps, documents."""
        self._require_company(company)
        if scheme_id not in {s["id"] for s in list_schemes()}:
            raise CreditError("unknown_scheme", "Unknown scheme", 404)
        as_of = self._today()
        results, classification, digest = self._evaluate_all(company, as_of)
        result = next(r for r in results if r["scheme_id"] == scheme_id)
        if result["status"] == "NOT_AVAILABLE_ON_DATE":
            raise CreditError("scheme_not_available", result["message"], 409)
        return {"company": company, "as_of": as_of.isoformat(), "facts_digest": digest, "classification": classification,
                "facts": self.get_facts(company)["facts"], "assessment": result, "readiness": self.readiness(company)}
