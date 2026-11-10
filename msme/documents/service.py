"""Document intelligence service: ingest -> process -> review -> approve.

State machine (one row per distinct file content):

    uploaded --process--> processing --> extracted | needs_review | failed
    extracted|needs_review --approve--> approved        (blocked while a CONFIRMED issue is open)
    any extracted state --correction--> needs_review    (an approval never survives an edit)
    failed --process (attempts < MAX_ATTEMPTS)--> processing
    processing older than STALE_PROCESSING is reclaimable (crash recovery)

Guarantees
  * Idempotent ingest: identical bytes -> the same document (UNIQUE sha256), never a second record.
  * Exactly-one processor: a compare-and-set UPDATE ... RETURNING claims the job.
  * original_value is immutable; every human change is an append-only correction row with who/why/when.
  * Corrections survive reprocessing; the machine's newest reading is kept beside them.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from lambda_erp.database import get_db
from msme.security.upload_validation import UploadRejected, validate_upload

from . import checks
from .extraction import (SEVERITY_CONFIRMED, SEVERITY_HEURISTIC, ExtractionOutcome, Extractor, bbox_to_json,
                         scrub)
from .storage import DocumentStorage, LocalDocumentStorage, StorageError, key_for, sha256_hex

MAX_BYTES = 10 * 1024 * 1024
MAX_ATTEMPTS = 3
STALE_PROCESSING = timedelta(minutes=15)
ALLOWED_MIMES = {"application/pdf", "image/png", "image/jpeg"}
EXT_TO_MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
FIELD_PATH = re.compile(r"^[A-Za-z_]\w*(\[\d+\])?(\.[A-Za-z_]\w*(\[\d+\])?)*$")
OPEN_STATES = ("extracted", "needs_review", "approved")
_PUBLIC_DOC_COLUMNS = (
    "id, filename, mime_type, size_bytes, sha256, uploaded_by, status, document_type, business_key, "
    "duplicate_of, attempts, error_code, error_message, extractor, extractor_version, approved_by, "
    "approved_at, created_at, updated_at, processed_at"
)


class DocumentError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def _ts(moment: Optional[datetime] = None, spec: str = "seconds") -> str:
    return (moment or datetime.now(timezone.utc)).isoformat(timespec=spec)


def _clean_filename(name: str) -> str:
    base = (name or "document").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable())
    return base[:200] or "document"


class DocumentService:
    def __init__(self, extractor: Extractor, storage: DocumentStorage | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.extractor = extractor
        self.storage = storage or LocalDocumentStorage()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        return _ts(self._clock())

    def _row(self, document_id: str) -> dict:
        rows = get_db().sql(f'SELECT * FROM "MSME Document" WHERE id = ?', [document_id])
        if not rows:
            raise DocumentError("not_found", "Document not found", 404)
        return dict(rows[0])

    def _by_sha(self, sha: str) -> Optional[dict]:
        rows = get_db().sql('SELECT * FROM "MSME Document" WHERE sha256 = ?', [sha])
        return dict(rows[0]) if rows else None

    @staticmethod
    def _public(row: dict) -> dict:
        row = dict(row)
        row.pop("storage_key", None)
        return row

    # ------------------------------------------------------------------ ingest
    def ingest(self, user_name: str, filename: str, data: bytes, declared_mime: str) -> tuple[dict, bool]:
        """Validate, store and register a file. Returns (document, duplicate)."""
        if not data:
            raise DocumentError("empty_file", "Empty file.", 400)
        if len(data) > MAX_BYTES:
            raise DocumentError("file_too_large", f"File too large. Maximum {MAX_BYTES // (1024 * 1024)} MB.", 413)
        try:
            mime = validate_upload(declared_mime, filename or "", data, ALLOWED_MIMES, EXT_TO_MIME)
        except UploadRejected as exc:
            raise DocumentError("invalid_file", str(exc), 400) from exc
        if mime not in ALLOWED_MIMES:
            raise DocumentError("unsupported_type", "Only PDF, PNG and JPEG documents are supported.", 415)

        sha = sha256_hex(data)
        existing = self._by_sha(sha)
        if existing:
            return self._public(existing), True

        key = key_for(sha, mime)
        self.storage.put(key, data)
        db = get_db()
        document_id = "MDOC-" + uuid.uuid4().hex[:12]
        stamp = self._now()
        created = _ts(self._clock(), "microseconds")   # upload order decides which duplicate is the original
        try:
            db.insert("MSME Document", {
                "id": document_id, "filename": _clean_filename(filename), "mime_type": mime,
                "size_bytes": len(data), "sha256": sha, "storage_key": key, "uploaded_by": user_name,
                "status": "uploaded", "attempts": 0, "created_at": created, "updated_at": stamp})
        except Exception:  # noqa: BLE001 - concurrent identical upload loses the UNIQUE race
            try:
                db.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            existing = self._by_sha(sha)
            if existing:
                return self._public(existing), True
            raise
        return self._public(self._row(document_id)), False

    # ------------------------------------------------------------------ process
    def process(self, document_id: str, force: bool = False) -> dict:
        """Claim and run extraction. Safe to call repeatedly and concurrently."""
        db = get_db()
        doc = self._row(document_id)
        states = ["uploaded", "failed"] + (list(OPEN_STATES) if force else [])
        placeholders = ", ".join("?" for _ in states)
        stale_before = _ts(self._clock() - STALE_PROCESSING)
        claimed = db.sql(
            'UPDATE "MSME Document" SET status = \'processing\', attempts = attempts + 1, updated_at = ?, '
            'error_code = NULL, error_message = NULL WHERE id = ? AND ('
            f'(status IN ({placeholders}) AND (attempts < ? OR ? = 1)) '
            "OR (status = 'processing' AND updated_at < ?)) RETURNING id",
            [self._now(), document_id, *states, MAX_ATTEMPTS, 1 if force else 0, stale_before])
        db.conn.commit()
        if not claimed:
            if doc["status"] == "failed" and doc["attempts"] >= MAX_ATTEMPTS:
                raise DocumentError("max_attempts", "Automatic retries are exhausted; a manager must reprocess "
                                    "explicitly.", 409)
            return self.get_document(document_id)  # already processed or being processed: no-op

        try:
            data = self.storage.get(doc["storage_key"])
            if sha256_hex(data) != doc["sha256"]:
                return self._fail(document_id, "storage_corrupt", "Stored file no longer matches its checksum")
            with self.storage.open_local(doc["storage_key"]) as path:
                outcome = self.extractor.extract(path)
        except StorageError as exc:
            return self._fail(document_id, "storage_error", scrub(exc))
        except Exception as exc:  # noqa: BLE001 - never leave a claimed job stuck in 'processing'
            return self._fail(document_id, "extractor_error", scrub(exc))
        return self._persist(document_id, outcome)

    def _fail(self, document_id: str, code: str, message: str) -> dict:
        db = get_db()
        db.sql('UPDATE "MSME Document" SET status = \'failed\', error_code = ?, error_message = ?, updated_at = ? '
               "WHERE id = ?", [code, scrub(message), self._now(), document_id])
        db.conn.commit()
        return self.get_document(document_id)

    def _persist(self, document_id: str, outcome: ExtractionOutcome) -> dict:
        if outcome.status == "failed":
            db = get_db()
            db.sql('UPDATE "MSME Document" SET status = \'failed\', error_code = ?, error_message = ?, '
                   'extractor = ?, extractor_version = ?, document_type = ?, updated_at = ? WHERE id = ?',
                   [outcome.error_code or "extraction_failed", scrub(outcome.error_message or "Extraction failed"),
                    outcome.extractor, outcome.extractor_version, outcome.document_type, self._now(), document_id])
            db.conn.commit()
            return self.get_document(document_id)

        db = get_db()
        stamp = self._now()
        with db.atomic():
            seen: set[str] = set()
            existing = {r["field_path"]: dict(r) for r in db.sql(
                'SELECT * FROM "MSME Extracted Field" WHERE document_id = ?', [document_id])}
            for f in outcome.fields:
                seen.add(f.path)
                row = existing.get(f.path)
                bbox = bbox_to_json(f.bbox)
                if row is None:
                    db.insert("MSME Extracted Field", {
                        "id": "MFLD-" + uuid.uuid4().hex[:12], "document_id": document_id, "field_path": f.path,
                        "original_value": f.value, "machine_value": f.value, "current_value": f.value,
                        "confidence": f.confidence, "page": f.page, "quote": f.quote, "bbox_json": bbox,
                        "location_status": f.location_status, "corrected": 0, "created_at": stamp,
                        "updated_at": stamp})
                else:
                    current = row["current_value"] if row["corrected"] else f.value
                    db.sql('UPDATE "MSME Extracted Field" SET machine_value = ?, current_value = ?, confidence = ?, '
                           "page = ?, quote = ?, bbox_json = ?, location_status = ?, updated_at = ? WHERE id = ?",
                           [f.value, current, f.confidence, f.page, f.quote, bbox, f.location_status, stamp, row["id"]])
            for path, row in existing.items():
                if path not in seen and not row["corrected"]:
                    db.sql('DELETE FROM "MSME Extracted Field" WHERE id = ?', [row["id"]])
            db.sql('DELETE FROM "MSME Validation Issue" WHERE document_id = ? AND source IN (\'docket\', \'msme\')',
                   [document_id])
            for issue in outcome.issues:
                self._add_issue(document_id, issue.code, issue.severity, issue.message, issue.field_path, "docket",
                                stamp)
            db.sql('UPDATE "MSME Document" SET status = ?, document_type = ?, extractor = ?, extractor_version = ?, '
                   'error_code = NULL, error_message = NULL, approved_by = NULL, approved_at = NULL, '
                   "processed_at = ?, updated_at = ? WHERE id = ?",
                   [outcome.status, outcome.document_type, outcome.extractor, outcome.extractor_version, stamp, stamp,
                    document_id])
            self._recheck(document_id, stamp, floor_status=outcome.status)
        return self.get_document(document_id)

    def _add_issue(self, document_id, code, severity, message, field_path, source, stamp, ack=None) -> None:
        row = {"id": "MISS-" + uuid.uuid4().hex[:12], "document_id": document_id, "code": code,
               "severity": severity, "message": message, "field_path": field_path, "source": source,
               "created_at": stamp}
        if ack:
            row.update(acknowledged_by=ack[0], acknowledged_at=ack[1], acknowledgement_reason=ack[2])
        get_db().insert("MSME Validation Issue", row)

    def _recheck(self, document_id: str, stamp: str, floor_status: Optional[str] = None) -> None:
        """Recompute deterministic issues, business key and duplicate flag from CURRENT values."""
        db = get_db()
        rows = db.sql('SELECT * FROM "MSME Extracted Field" WHERE document_id = ?', [document_id])
        values = {r["field_path"]: r["current_value"] for r in rows if r["current_value"] is not None}
        meta = {r["field_path"]: checks.FieldMeta(r["confidence"], r["location_status"]) for r in rows}
        acks = {(r["code"], r["field_path"], r["message"]): (r["acknowledged_by"], r["acknowledged_at"],
                                                              r["acknowledgement_reason"])
                for r in db.sql('SELECT code, field_path, message, acknowledged_by, acknowledged_at, '
                                'acknowledgement_reason FROM "MSME Validation Issue" WHERE document_id = ? '
                                "AND source = 'msme' AND acknowledged_by IS NOT NULL", [document_id])}
        db.sql('DELETE FROM "MSME Validation Issue" WHERE document_id = ? AND source = \'msme\'', [document_id])
        for issue in checks.validate(values, meta, today=self._clock().date()):
            self._add_issue(document_id, issue.code, issue.severity, issue.message, issue.field_path, "msme", stamp,
                            ack=acks.get((issue.code, issue.field_path, issue.message)))

        key = checks.normalise_business_key(values)
        old_key = self._row(document_id)["business_key"]
        db.sql('UPDATE "MSME Document" SET business_key = ?, updated_at = ? WHERE id = ?', [key, stamp, document_id])
        if not key:
            db.sql('UPDATE "MSME Document" SET duplicate_of = NULL WHERE id = ?', [document_id])
        changed = self._reconcile_duplicates([old_key, key], stamp, skip_id=document_id)
        self._sync_duplicate_issue(document_id, stamp)
        for other_id in changed:
            self._sync_duplicate_issue(other_id, stamp)

        issues = db.sql('SELECT severity FROM "MSME Validation Issue" WHERE document_id = ?', [document_id])
        status = floor_status or self._row(document_id)["status"]
        if status in ("extracted", "approved") and issues:
            status = "needs_review"
        if status in OPEN_STATES or status == "needs_review":
            db.sql('UPDATE "MSME Document" SET status = ?, updated_at = ? WHERE id = ?', [status, stamp, document_id])

    def _reconcile_duplicates(self, keys, stamp: str, skip_id: str) -> list[str]:
        """Within each business-key group the EARLIEST upload is the original and every other member is a
        duplicate of it. Recomputed for the whole group whenever membership or a key changes, so the original
        can never end up pointing at its own copy. Returns the ids (other than skip_id) whose flag changed."""
        db, changed = get_db(), []
        for key in {k for k in keys if k}:
            members = db.sql('SELECT id, duplicate_of FROM "MSME Document" WHERE business_key = ? '
                             "AND status <> 'failed' ORDER BY created_at, id", [key])
            if not members:
                continue
            original = members[0]["id"]
            for m in members:
                wanted = None if m["id"] == original else original
                if m["duplicate_of"] != wanted:
                    db.sql('UPDATE "MSME Document" SET duplicate_of = ?, updated_at = ? WHERE id = ?',
                           [wanted, stamp, m["id"]])
                    if m["id"] != skip_id:
                        changed.append(m["id"])
        return changed

    def _sync_duplicate_issue(self, document_id: str, stamp: str) -> None:
        """Make the DUPLICATE_CANDIDATE issue match duplicate_of. Becoming a duplicate revokes an approval;
        ceasing to be one removes the issue but never changes the status on its own."""
        db = get_db()
        row = self._row(document_id)
        existing = db.sql('SELECT id, message FROM "MSME Validation Issue" WHERE document_id = ? '
                          "AND code = 'DUPLICATE_CANDIDATE'", [document_id])
        original = row["duplicate_of"]
        if not original:
            for r in existing:
                db.sql('DELETE FROM "MSME Validation Issue" WHERE id = ?', [r["id"]])
            return
        message = f"Same supplier, invoice number, date and total as {original}"
        if existing and existing[0]["message"] == message:
            return
        for r in existing:
            db.sql('DELETE FROM "MSME Validation Issue" WHERE id = ?', [r["id"]])
        self._add_issue(document_id, "DUPLICATE_CANDIDATE", SEVERITY_HEURISTIC, message, None, "msme", stamp)
        if row["status"] in ("extracted", "approved"):
            db.sql('UPDATE "MSME Document" SET status = \'needs_review\', approved_by = NULL, approved_at = NULL, '
                   "updated_at = ? WHERE id = ?", [stamp, document_id])

    # ------------------------------------------------------------------ review
    def correct_field(self, document_id: str, field_path: str, new_value: str, reason: str, user_name: str) -> dict:
        doc = self._row(document_id)
        if doc["status"] not in OPEN_STATES:
            raise DocumentError("not_reviewable", f"Document is '{doc['status']}' and cannot be corrected", 409)
        if not FIELD_PATH.match(field_path or "") or len(field_path) > 120:
            raise DocumentError("invalid_field", "Invalid field path", 422)
        value = (new_value or "").strip()
        if not value or len(value) > 1000:
            raise DocumentError("invalid_value", "Value must be 1-1000 characters", 422)
        reason = (reason or "").strip()
        if not reason or len(reason) > 500:
            raise DocumentError("reason_required", "A reason of 1-500 characters is required", 422)

        db = get_db()
        stamp = self._now()
        with db.atomic():
            rows = db.sql('SELECT * FROM "MSME Extracted Field" WHERE document_id = ? AND field_path = ?',
                          [document_id, field_path])
            if rows:
                row = dict(rows[0])
                if row["current_value"] == value:
                    raise DocumentError("no_change", "The value is unchanged", 422)
                old = row["current_value"]
                db.sql('UPDATE "MSME Extracted Field" SET current_value = ?, corrected = 1, updated_at = ? WHERE id = ?',
                       [value, stamp, row["id"]])
            else:
                old = None
                db.insert("MSME Extracted Field", {
                    "id": "MFLD-" + uuid.uuid4().hex[:12], "document_id": document_id, "field_path": field_path,
                    "original_value": None, "machine_value": None, "current_value": value, "location_status": "manual",
                    "corrected": 1, "created_at": stamp, "updated_at": stamp})
            db.insert("MSME Field Correction", {
                "id": "MCOR-" + uuid.uuid4().hex[:12], "document_id": document_id, "field_path": field_path,
                "old_value": old, "new_value": value, "reason": reason, "corrected_by": user_name,
                "corrected_at": stamp})
            db.sql('UPDATE "MSME Document" SET status = \'needs_review\', approved_by = NULL, approved_at = NULL, '
                   "updated_at = ? WHERE id = ?", [stamp, document_id])
            self._recheck(document_id, stamp, floor_status="needs_review")
        return self.get_document(document_id)

    def acknowledge_issues(self, document_id: str, code: str, field_path: Optional[str], reason: str,
                           user_name: str) -> dict:
        """Accept a CONFIRMED issue with a recorded reason (e.g. a misprinted supplier invoice, or rows a human
        verified by eye that the engine could not cite). It stays visible; it just stops blocking approval."""
        doc = self._row(document_id)
        if doc["status"] not in ("extracted", "needs_review"):
            raise DocumentError("not_reviewable", f"Document is '{doc['status']}' and cannot be reviewed", 409)
        reason = (reason or "").strip()
        if not reason or len(reason) > 500:
            raise DocumentError("reason_required", "A reason of 1-500 characters is required", 422)
        db = get_db()
        query = ('SELECT id FROM "MSME Validation Issue" WHERE document_id = ? AND code = ? AND severity = ? '
                 "AND acknowledged_by IS NULL")
        params: list[Any] = [document_id, code, SEVERITY_CONFIRMED]
        if field_path:
            query += " AND field_path = ?"
            params.append(field_path)
        rows = db.sql(query, params)
        if not rows:
            raise DocumentError("issue_not_found", "No open confirmed issue matches", 404)
        stamp = self._now()
        with db.atomic():
            for r in rows:
                db.sql('UPDATE "MSME Validation Issue" SET acknowledged_by = ?, acknowledged_at = ?, '
                       "acknowledgement_reason = ? WHERE id = ?", [user_name, stamp, reason, r["id"]])
            db.sql('UPDATE "MSME Document" SET updated_at = ? WHERE id = ?', [stamp, document_id])
        return self.get_document(document_id)

    def approve(self, document_id: str, user_name: str) -> dict:
        doc = self._row(document_id)
        if doc["status"] not in ("extracted", "needs_review"):
            raise DocumentError("not_approvable", f"Document is '{doc['status']}' and cannot be approved", 409)
        open_confirmed = get_db().sql(
            'SELECT code FROM "MSME Validation Issue" WHERE document_id = ? AND severity = ? '
            "AND acknowledged_by IS NULL", [document_id, SEVERITY_CONFIRMED])
        if open_confirmed:
            codes = sorted({r["code"] for r in open_confirmed})
            raise DocumentError("confirmed_issues_open",
                                "Correct or acknowledge confirmed issues first: " + ", ".join(codes), 409)
        stamp = self._now()
        db = get_db()
        db.sql('UPDATE "MSME Document" SET status = \'approved\', approved_by = ?, approved_at = ?, updated_at = ? '
               "WHERE id = ?", [user_name, stamp, stamp, document_id])
        db.conn.commit()
        return self.get_document(document_id)

    # ------------------------------------------------------------------ reads
    def get_document(self, document_id: str) -> dict:
        doc = self._public(self._row(document_id))
        db = get_db()
        doc["fields"] = [dict(r) for r in db.sql(
            'SELECT field_path, original_value, machine_value, current_value, confidence, page, quote, bbox_json, '
            'location_status, corrected FROM "MSME Extracted Field" WHERE document_id = ? ORDER BY field_path',
            [document_id])]
        for f in doc["fields"]:
            f["bbox"] = json.loads(f.pop("bbox_json")) if f.get("bbox_json") else None
            f.pop("bbox_json", None)
            f["corrected"] = bool(f["corrected"])
        doc["issues"] = [dict(r) for r in db.sql(
            'SELECT code, severity, message, field_path, source, acknowledged_by, acknowledged_at, '
            'acknowledgement_reason FROM "MSME Validation Issue" WHERE document_id = ? '
            "ORDER BY severity, code, field_path", [document_id])]
        return doc

    def history(self, document_id: str, field_path: Optional[str] = None) -> list[dict]:
        self._row(document_id)
        query = ('SELECT field_path, old_value, new_value, reason, corrected_by, corrected_at '
                 'FROM "MSME Field Correction" WHERE document_id = ?')
        params: list[Any] = [document_id]
        if field_path:
            query += " AND field_path = ?"
            params.append(field_path)
        return [dict(r) for r in get_db().sql(query + " ORDER BY corrected_at, id", params)]

    def list_documents(self, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        where, params = "", []
        if status:
            where, params = " WHERE status = ?", [status]
        db = get_db()
        total = db.sql(f'SELECT COUNT(*) AS n FROM "MSME Document"{where}', params)[0]["n"]
        rows = db.sql(f'SELECT {_PUBLIC_DOC_COLUMNS} FROM "MSME Document"{where} ORDER BY created_at DESC, id '
                      "LIMIT ? OFFSET ?", [*params, limit, offset])
        return {"total": total, "rows": [dict(r) for r in rows]}

    def read_original(self, document_id: str) -> tuple[bytes, str, str]:
        doc = self._row(document_id)
        data = self.storage.get(doc["storage_key"])
        if sha256_hex(data) != doc["sha256"]:
            raise DocumentError("storage_corrupt", "Stored file no longer matches its checksum", 500)
        return data, doc["mime_type"], doc["filename"]

    # ------------------------------------------------------------------ operations
    def sweep(self, limit: int = 10) -> dict:
        """Process queued ('uploaded') and stale ('processing') documents; for a scheduler or admin."""
        stale_before = _ts(self._clock() - STALE_PROCESSING)
        rows = get_db().sql(
            'SELECT id FROM "MSME Document" WHERE status = \'uploaded\' OR (status = \'processing\' AND updated_at < ?) '
            "ORDER BY created_at LIMIT ?", [stale_before, max(1, min(int(limit), 100))])
        results = {"processed": 0, "failed": 0}
        for r in rows:
            try:
                out = self.process(r["id"])
            except DocumentError:
                results["failed"] += 1
                continue
            results["failed" if out["status"] == "failed" else "processed"] += 1
        return results
