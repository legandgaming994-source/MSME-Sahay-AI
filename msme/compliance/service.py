"""Compliance workspace service.

Task lifecycle (preparation only; nothing here files a return):

    open -> in_preparation -> in_review -> approved_for_filing -> filing_recorded
                 ^                |
                 +-- rejected ----+     (a new workpaper revision after approval returns the task to in_preparation)

`filing_recorded` means the USER recorded their own portal reference and date. It is not verified against any
government system and is labelled as such everywhere.

Guarantees
  * Idempotent generation: unique (company, fy, form, period).
  * Workpapers are immutable once submitted; revisions are new versions.
  * Maker-checker: the approver cannot have contributed to the workpaper (waivable only by explicit env policy,
    and a waiver is written into the audit event).
  * Approval is bound to a digest of the figures it was based on; changed source documents block approval/recording.
  * Every state change appends a hash-chained audit event in the same transaction.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from lambda_erp.database import get_db
from msme.documents.checks import gstin_problem
from msme.rules import RuleError, gst_deadlines

from . import audit
from .figures import compute_figures, period_range

FILER_TYPES = ("regular_monthly", "qrmp", "composition")
ITEM_STATES = ("pending", "done", "na")
REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/\-]{2,59}$")
_CHECKLISTS = Path(__file__).parent / "data" / "checklists.json"


class ComplianceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


@lru_cache(maxsize=1)
def _checklists() -> dict:
    return json.loads(_CHECKLISTS.read_text(encoding="utf-8"))


def _is_unique_violation(exc: Exception) -> bool:
    """Retryable conflict: duplicate key (e.g. audit seq) or a PostgreSQL deadlock/serialization abort."""
    return audit.is_retryable(exc)


def require_distinct_approver() -> bool:
    return os.environ.get("MSME_REQUIRE_DISTINCT_APPROVER", "1").strip() not in ("0", "false", "no")


class ComplianceService:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ plumbing
    def _ts(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _today(self) -> date:
        return self._clock().date()

    def _tx(self, fn: Callable[[], Any]) -> Any:
        """Run fn in one transaction; retry when a concurrent audit append took the same sequence number."""
        db = get_db()
        for attempt in range(10):
            try:
                with db.atomic():
                    return fn()
            except ComplianceError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_unique_violation(exc):
                    time.sleep(0.005 * (attempt + 1))
                    continue
                raise
        raise ComplianceError("busy", "The system is busy; please retry", 503)

    def _audit(self, actor: str, action: str, entity_type: str, entity_id: str, detail: dict | None = None) -> None:
        audit.append(get_db(), self._ts(), actor, action, entity_type, entity_id, detail)

    def _task(self, task_id: str) -> dict:
        rows = get_db().sql('SELECT * FROM "MSME Compliance Task" WHERE id = ?', [task_id])
        if not rows:
            raise ComplianceError("not_found", "Task not found", 404)
        return dict(rows[0])

    def _wp(self, workpaper_id: str) -> dict:
        rows = get_db().sql('SELECT * FROM "MSME Workpaper" WHERE id = ?', [workpaper_id])
        if not rows:
            raise ComplianceError("not_found", "Workpaper not found", 404)
        return dict(rows[0])

    def _decorate_task(self, row: dict) -> dict:
        row = dict(row)
        due = date.fromisoformat(row["due_date"])
        row["optional"] = bool(row["optional"])
        row["days_to_due"] = (due - self._today()).days
        row["overdue"] = row["status"] != "filing_recorded" and due < self._today()
        row["filing_note_label"] = ("Recorded by the user; not verified against the GST portal"
                                    if row["status"] == "filing_recorded" else None)
        return row

    # ------------------------------------------------------------------ profile & overrides
    def get_profile(self, company: str) -> Optional[dict]:
        rows = get_db().sql('SELECT * FROM "MSME Compliance Profile" WHERE company = ?', [company])
        return dict(rows[0]) if rows else None

    def set_profile(self, company: str, filer_type: str, state_category: Optional[str], gstin: Optional[str],
                    user: str) -> dict:
        db = get_db()
        if not db.sql('SELECT name FROM "Company" WHERE name = ?', [company]):
            raise ComplianceError("company_not_found", "Unknown company", 404)
        if filer_type not in FILER_TYPES:
            raise ComplianceError("invalid_filer_type", f"filer_type must be one of {', '.join(FILER_TYPES)}", 422)
        if state_category not in (None, "X", "Y"):
            raise ComplianceError("invalid_state_category", "state_category must be 'X' or 'Y'", 422)
        if filer_type == "qrmp" and state_category is None:
            raise ComplianceError("state_category_required",
                                  "QRMP filers must state their GSTR-3B state category (X or Y); it is never inferred", 422)
        gstin = re.sub(r"\s+", "", gstin or "").upper() or None
        if gstin and (problem := gstin_problem(gstin)):
            raise ComplianceError("invalid_gstin", f"GSTIN {problem}", 422)
        stamp = self._ts()

        def work():
            if self.get_profile(company):
                db.sql('UPDATE "MSME Compliance Profile" SET filer_type = ?, state_category = ?, gstin = ?, '
                       "updated_by = ?, updated_at = ? WHERE company = ?",
                       [filer_type, state_category, gstin, user, stamp, company])
            else:
                db.insert("MSME Compliance Profile", {"company": company, "filer_type": filer_type,
                                                      "state_category": state_category, "gstin": gstin,
                                                      "updated_by": user, "updated_at": stamp})
            self._audit(user, "profile.set", "profile", company,
                        {"filer_type": filer_type, "state_category": state_category, "gstin": gstin})
        self._tx(work)
        return self.get_profile(company)  # type: ignore[return-value]

    def add_override(self, company: str, form: str, period: str, due_date: str, source: str, user: str) -> dict:
        form, source = (form or "").strip(), (source or "").strip()
        if not form or len(form) > 20:
            raise ComplianceError("invalid_form", "form is required (max 20 characters)", 422)
        try:
            period_range(period)
            due = date.fromisoformat(due_date)
        except ValueError as exc:
            raise ComplianceError("invalid_value", f"Invalid period or due date: {exc}", 422)
        if not (5 <= len(source) <= 300):
            raise ComplianceError("source_required",
                                  "Cite the notification or notice (5-300 characters) that extends this deadline", 422)
        stamp, db = self._ts(), get_db()

        def work():
            if db.sql('SELECT id FROM "MSME Deadline Override" WHERE company = ? AND form = ? AND period = ?',
                      [company, form, period]):
                db.sql('UPDATE "MSME Deadline Override" SET due_date = ?, source = ?, entered_by = ?, entered_at = ? '
                       "WHERE company = ? AND form = ? AND period = ?",
                       [due.isoformat(), source, user, stamp, company, form, period])
            else:
                db.insert("MSME Deadline Override", {"id": "MOVR-" + uuid.uuid4().hex[:12], "company": company,
                          "form": form, "period": period, "due_date": due.isoformat(), "source": source,
                          "entered_by": user, "entered_at": stamp})
            moved = db.sql('SELECT id FROM "MSME Compliance Task" WHERE company = ? AND form = ? AND period = ? '
                           "AND status <> 'filing_recorded'", [company, form, period])
            db.sql('UPDATE "MSME Compliance Task" SET due_date = ?, basis = \'EXTENSION_OVERRIDE\', updated_at = ? '
                   "WHERE company = ? AND form = ? AND period = ? AND status <> 'filing_recorded'",
                   [due.isoformat(), stamp, company, form, period])
            self._audit(user, "override.set", "company", company, {"form": form, "period": period,
                        "due_date": due.isoformat(), "source": source, "tasks_updated": len(moved),
                        "verification": "USER_SUPPLIED"})
        self._tx(work)
        return {"company": company, "form": form, "period": period, "due_date": due.isoformat(), "source": source,
                "verification": "USER_SUPPLIED"}

    # ------------------------------------------------------------------ tasks
    def generate_tasks(self, company: str, fy_start_year: int, user: str) -> dict:
        profile = self.get_profile(company)
        if not profile:
            raise ComplianceError("profile_required", "Set the compliance profile (filer type) first", 409)
        db = get_db()
        overrides = [dict(r) for r in db.sql(
            'SELECT form, period, due_date, source FROM "MSME Deadline Override" WHERE company = ?', [company])]
        try:
            deadlines = gst_deadlines(profile["filer_type"], int(fy_start_year), profile["state_category"], overrides)
        except RuleError as exc:
            raise ComplianceError("rule_error", str(exc), 422)
        stamp = self._ts()
        counts = {"created": 0, "updated": 0, "unchanged": 0}

        def work():
            for k in counts:
                counts[k] = 0
            for d in deadlines:
                rows = db.sql('SELECT * FROM "MSME Compliance Task" WHERE company = ? AND fy_start_year = ? '
                              "AND form = ? AND period = ?", [company, int(fy_start_year), d.form, d.period])
                if not rows:
                    db.insert("MSME Compliance Task", {
                        "id": "MTSK-" + uuid.uuid4().hex[:12], "company": company, "fy_start_year": int(fy_start_year),
                        "form": d.form, "period": d.period, "due_date": d.due_date.isoformat(), "basis": d.basis,
                        "rule_version": d.rule_version, "optional": 1 if d.optional else 0, "status": "open",
                        "created_at": stamp, "updated_at": stamp})
                    counts["created"] += 1
                    continue
                row = rows[0]
                if row["status"] != "filing_recorded" and (
                        row["due_date"] != d.due_date.isoformat() or row["basis"] != d.basis
                        or row["rule_version"] != d.rule_version):
                    db.sql('UPDATE "MSME Compliance Task" SET due_date = ?, basis = ?, rule_version = ?, updated_at = ? '
                           "WHERE id = ?", [d.due_date.isoformat(), d.basis, d.rule_version, stamp, row["id"]])
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
            if counts["created"] or counts["updated"]:
                self._audit(user, "tasks.generate", "company", company,
                            {"fy_start_year": int(fy_start_year), "filer_type": profile["filer_type"], **counts})
        self._tx(work)
        return {"company": company, "fy_start_year": int(fy_start_year), **counts,
                "note": "Statutory default dates. Government extensions are not applied unless entered as overrides."}

    def list_tasks(self, company: Optional[str] = None, fy_start_year: Optional[int] = None,
                   status: Optional[str] = None, limit: int = 100, offset: int = 0) -> dict:
        where, params = [], []
        for col, val in (("company", company), ("fy_start_year", fy_start_year), ("status", status)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        db = get_db()
        total = db.sql(f'SELECT COUNT(*) AS n FROM "MSME Compliance Task"{clause}', params)[0]["n"]
        rows = db.sql(f'SELECT * FROM "MSME Compliance Task"{clause} ORDER BY due_date, form, period LIMIT ? OFFSET ?',
                      [*params, limit, offset])
        return {"total": total, "rows": [self._decorate_task(dict(r)) for r in rows]}

    def upcoming(self, company: str, days: int = 14) -> list[dict]:
        """Actionable reminders: overdue and due-soon work that is not yet recorded as filed."""
        horizon = (self._today() + timedelta(days=max(0, min(int(days), 366)))).isoformat()
        rows = get_db().sql(
            'SELECT * FROM "MSME Compliance Task" WHERE company = ? AND status <> \'filing_recorded\' AND due_date <= ? '
            "AND NOT (optional = 1 AND status = 'open') ORDER BY due_date, form", [company, horizon])
        return [self._decorate_task(dict(r)) for r in rows]

    def get_task(self, task_id: str) -> dict:
        task = self._decorate_task(self._task(task_id))
        db = get_db()
        task["workpapers"] = [dict(r) for r in db.sql(
            "SELECT id, version, status, created_by, created_at, submitted_by, submitted_at, decided_by, decided_at, "
            'decision_note FROM "MSME Workpaper" WHERE task_id = ? ORDER BY version', [task_id])]
        ids = [task_id] + [w["id"] for w in task["workpapers"]]
        marks = ", ".join("?" for _ in ids)
        task["audit"] = [dict(r) for r in db.sql(
            f'SELECT seq, ts, actor, action, entity_type, entity_id, detail FROM "MSME Audit Event" '
            f"WHERE entity_id IN ({marks}) ORDER BY seq", ids)]
        return task

    # ------------------------------------------------------------------ workpapers
    @staticmethod
    def _template(form: str) -> list[dict]:
        data = _checklists()
        items = data["forms"].get(form, data["default"])
        return [{"key": i["key"], "label": i["label"], "required": bool(i["required"]), "status": "pending",
                 "note": "", "document_ids": []} for i in items]

    def _figures(self, task: dict) -> dict:
        profile = self.get_profile(task["company"])
        return compute_figures(get_db(), profile["gstin"] if profile else None, task["period"])

    def _public_wp(self, row: dict) -> dict:
        row = dict(row)
        row["content"] = json.loads(row.pop("content_json"))
        row["contributors"] = json.loads(row.pop("contributors_json"))
        row["content"]["checklist_source"] = _checklists()["source"]
        row["content"]["checklist_notice"] = _checklists()["notice"]
        return row

    def get_workpaper(self, workpaper_id: str) -> dict:
        wp = self._public_wp(self._wp(workpaper_id))
        wp["task"] = self._decorate_task(self._task(wp["task_id"]))
        return wp

    def open_workpaper(self, task_id: str, user: str) -> dict:
        task = self._task(task_id)
        if task["status"] == "filing_recorded":
            raise ComplianceError("task_closed", "A filing has been recorded for this task", 409)
        latest = get_db().sql('SELECT * FROM "MSME Workpaper" WHERE task_id = ? ORDER BY version DESC LIMIT 1', [task_id])
        latest = dict(latest[0]) if latest else None
        if latest and latest["status"] == "draft":
            return self.get_workpaper(latest["id"])
        if latest and latest["status"] == "submitted":
            raise ComplianceError("workpaper_in_review", "The latest workpaper is awaiting review", 409)
        figures = self._figures(task)
        checklist = (json.loads(latest["content_json"])["checklist"] if latest else self._template(task["form"]))
        wp_id, stamp, db = "MWP-" + uuid.uuid4().hex[:12], self._ts(), get_db()
        version = (latest["version"] + 1) if latest else 1

        def work():
            db.insert("MSME Workpaper", {
                "id": wp_id, "task_id": task_id, "version": version, "status": "draft",
                "content_json": json.dumps({"checklist": checklist, "figures": figures, "notes": ""}),
                "figures_digest": figures["digest"], "contributors_json": json.dumps([user]),
                "created_by": user, "created_at": stamp, "updated_at": stamp})
            db.sql('UPDATE "MSME Compliance Task" SET status = \'in_preparation\', updated_at = ? WHERE id = ?',
                   [stamp, task_id])
            self._audit(user, "workpaper.open", "workpaper", wp_id, {
                "task_id": task_id, "version": version, "supersedes": latest["id"] if latest else None,
                "supersedes_status": latest["status"] if latest else None})
        self._tx(work)
        return self.get_workpaper(wp_id)

    def update_draft(self, workpaper_id: str, user: str, checklist: Optional[list[dict]] = None,
                     notes: Optional[str] = None, refresh_figures: bool = False) -> dict:
        wp = self._wp(workpaper_id)
        if wp["status"] != "draft":
            raise ComplianceError("not_editable", f"Workpaper is '{wp['status']}'; open a new revision to change it", 409)
        content = json.loads(wp["content_json"])
        by_key = {i["key"]: i for i in content["checklist"]}
        db = get_db()
        for update in checklist or []:
            item = by_key.get(update.get("key"))
            if item is None:
                raise ComplianceError("unknown_item", f"Unknown checklist item {update.get('key')!r}", 422)
            if "status" in update:
                if update["status"] not in ITEM_STATES:
                    raise ComplianceError("invalid_status", f"status must be one of {', '.join(ITEM_STATES)}", 422)
                item["status"] = update["status"]
            if "note" in update:
                if len(update["note"] or "") > 500:
                    raise ComplianceError("note_too_long", "Item notes are limited to 500 characters", 422)
                item["note"] = update["note"] or ""
            if "document_ids" in update:
                ids = list(dict.fromkeys(update["document_ids"] or []))
                if len(ids) > 20:
                    raise ComplianceError("too_many_documents", "At most 20 documents per item", 422)
                for doc_id in ids:
                    if not db.sql('SELECT id FROM "MSME Document" WHERE id = ?', [doc_id]):
                        raise ComplianceError("unknown_document", f"Unknown document {doc_id}", 422)
                item["document_ids"] = ids
        if notes is not None:
            if len(notes) > 2000:
                raise ComplianceError("notes_too_long", "Notes are limited to 2000 characters", 422)
            content["notes"] = notes
        if refresh_figures:
            content["figures"] = self._figures(self._task(wp["task_id"]))
        contributors = json.loads(wp["contributors_json"])
        if user not in contributors:
            contributors.append(user)
        stamp = self._ts()

        def work():
            db.sql('UPDATE "MSME Workpaper" SET content_json = ?, figures_digest = ?, contributors_json = ?, '
                   "updated_at = ? WHERE id = ? AND status = 'draft'",
                   [json.dumps(content), content["figures"]["digest"], json.dumps(contributors), stamp, workpaper_id])
            self._audit(user, "workpaper.update", "workpaper", workpaper_id, {
                "items": sorted({u.get("key") for u in checklist or []}), "notes_changed": notes is not None,
                "figures_refreshed": bool(refresh_figures)})
        self._tx(work)
        return self.get_workpaper(workpaper_id)

    def submit_workpaper(self, workpaper_id: str, user: str) -> dict:
        wp = self._wp(workpaper_id)
        if wp["status"] != "draft":
            raise ComplianceError("not_submittable", f"Workpaper is '{wp['status']}'", 409)
        content = json.loads(wp["content_json"])
        missing = [i["key"] for i in content["checklist"] if i["required"] and i["status"] == "pending"]
        if missing:
            raise ComplianceError("checklist_incomplete", "Required items still pending: " + ", ".join(missing), 409)
        task = self._task(wp["task_id"])
        figures = self._figures(task)
        content["figures"] = figures
        contributors = json.loads(wp["contributors_json"])
        if user not in contributors:
            contributors.append(user)
        stamp, db = self._ts(), get_db()

        def work():
            done = db.sql('UPDATE "MSME Workpaper" SET status = \'submitted\', content_json = ?, figures_digest = ?, '
                          "contributors_json = ?, submitted_by = ?, submitted_at = ?, updated_at = ? "
                          "WHERE id = ? AND status = 'draft' RETURNING id",
                          [json.dumps(content), figures["digest"], json.dumps(contributors), user, stamp, stamp,
                           workpaper_id])
            if not done:
                raise ComplianceError("not_submittable", "Workpaper changed state concurrently", 409)
            db.sql('UPDATE "MSME Compliance Task" SET status = \'in_review\', updated_at = ? WHERE id = ?',
                   [stamp, task["id"]])
            self._audit(user, "workpaper.submit", "workpaper", workpaper_id,
                        {"task_id": task["id"], "version": wp["version"], "figures_digest": figures["digest"]})
        self._tx(work)
        return self.get_workpaper(workpaper_id)

    def approve_workpaper(self, workpaper_id: str, user: str) -> dict:
        wp = self._wp(workpaper_id)
        if wp["status"] != "submitted":
            raise ComplianceError("not_approvable", f"Workpaper is '{wp['status']}'", 409)
        contributors = json.loads(wp["contributors_json"])
        distinct = require_distinct_approver()
        if distinct and (user in contributors or user == wp["created_by"]):
            raise ComplianceError("maker_checker", "The approver must be someone who did not prepare this workpaper", 403)
        task = self._task(wp["task_id"])
        if self._figures(task)["digest"] != wp["figures_digest"]:
            raise ComplianceError("figures_stale", "The source documents changed after submission; reject and re-prepare", 409)
        stamp, db = self._ts(), get_db()

        def work():
            done = db.sql('UPDATE "MSME Workpaper" SET status = \'approved\', decided_by = ?, decided_at = ?, '
                          "updated_at = ? WHERE id = ? AND status = 'submitted' RETURNING id",
                          [user, stamp, stamp, workpaper_id])
            if not done:
                raise ComplianceError("not_approvable", "Workpaper changed state concurrently", 409)
            db.sql('UPDATE "MSME Compliance Task" SET status = \'approved_for_filing\', updated_at = ? WHERE id = ?',
                   [stamp, task["id"]])
            self._audit(user, "workpaper.approve", "workpaper", workpaper_id, {
                "task_id": task["id"], "version": wp["version"], "figures_digest": wp["figures_digest"],
                "maker_checker": "enforced" if distinct else "waived_by_policy"})
        self._tx(work)
        return self.get_workpaper(workpaper_id)

    def reject_workpaper(self, workpaper_id: str, user: str, note: str) -> dict:
        note = (note or "").strip()
        if not note or len(note) > 500:
            raise ComplianceError("reason_required", "A reason of 1-500 characters is required", 422)
        wp = self._wp(workpaper_id)
        if wp["status"] != "submitted":
            raise ComplianceError("not_rejectable", f"Workpaper is '{wp['status']}'", 409)
        stamp, db = self._ts(), get_db()

        def work():
            done = db.sql('UPDATE "MSME Workpaper" SET status = \'rejected\', decided_by = ?, decided_at = ?, '
                          "decision_note = ?, updated_at = ? WHERE id = ? AND status = 'submitted' RETURNING id",
                          [user, stamp, note, stamp, workpaper_id])
            if not done:
                raise ComplianceError("not_rejectable", "Workpaper changed state concurrently", 409)
            db.sql('UPDATE "MSME Compliance Task" SET status = \'in_preparation\', updated_at = ? WHERE id = ?',
                   [stamp, wp["task_id"]])
            self._audit(user, "workpaper.reject", "workpaper", workpaper_id,
                        {"task_id": wp["task_id"], "version": wp["version"], "reason": note})
        self._tx(work)
        return self.get_workpaper(workpaper_id)

    def record_filing(self, task_id: str, user: str, reference: str, filed_on: str, note: Optional[str] = None) -> dict:
        task = self._task(task_id)
        if task["status"] != "approved_for_filing":
            raise ComplianceError("not_approved", "Only a task whose workpaper is approved can have a filing recorded", 409)
        reference = (reference or "").strip()
        if not REFERENCE.match(reference):
            raise ComplianceError("invalid_reference", "Enter the reference shown by the portal (3-60 letters, digits, "
                                  "space . _ / -)", 422)
        try:
            filed = date.fromisoformat(filed_on)
        except ValueError:
            raise ComplianceError("invalid_date", "filed_on must be an ISO date", 422)
        if filed > self._today():
            raise ComplianceError("invalid_date", "filed_on cannot be in the future", 422)
        if note and len(note) > 300:
            raise ComplianceError("note_too_long", "Notes are limited to 300 characters", 422)
        latest = get_db().sql('SELECT * FROM "MSME Workpaper" WHERE task_id = ? ORDER BY version DESC LIMIT 1', [task_id])
        if not latest or latest[0]["status"] != "approved":
            raise ComplianceError("not_approved", "The latest workpaper is not approved", 409)
        if self._figures(task)["digest"] != latest[0]["figures_digest"]:
            raise ComplianceError("figures_stale", "Source documents changed after approval; prepare a new revision", 409)
        stamp, db = self._ts(), get_db()

        def work():
            done = db.sql('UPDATE "MSME Compliance Task" SET status = \'filing_recorded\', filing_reference = ?, '
                          "filed_on = ?, filing_note = ?, filing_recorded_by = ?, filing_recorded_at = ?, updated_at = ? "
                          "WHERE id = ? AND status = 'approved_for_filing' RETURNING id",
                          [reference, filed.isoformat(), note, user, stamp, stamp, task_id])
            if not done:
                raise ComplianceError("not_approved", "Task changed state concurrently", 409)
            self._audit(user, "filing.recorded_by_user", "task", task_id, {
                "reference": reference, "filed_on": filed.isoformat(), "verified_against_portal": False,
                "workpaper_id": latest[0]["id"]})
        self._tx(work)
        return self.get_task(task_id)

    # ------------------------------------------------------------------ audit
    def audit_events(self, entity_id: Optional[str] = None, limit: int = 100, offset: int = 0) -> list[dict]:
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        query = 'SELECT seq, ts, actor, action, entity_type, entity_id, detail, hash FROM "MSME Audit Event"'
        params: list[Any] = []
        if entity_id:
            query += " WHERE entity_id = ?"
            params.append(entity_id)
        return [dict(r) for r in get_db().sql(query + " ORDER BY seq DESC LIMIT ? OFFSET ?", [*params, limit, offset])]

    def verify_audit(self) -> dict:
        return {**audit.verify(get_db()), "head": audit.head(get_db())}
