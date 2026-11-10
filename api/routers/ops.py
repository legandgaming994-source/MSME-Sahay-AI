"""Operational endpoints.

  GET /api/ready       public, minimal. 200 only when the database answers, migrations are at head and document storage
                       is healthy; otherwise 503 listing only the names of the failing checks (no details). This is the
                       platform health check: it keeps a broken instance out of rotation.
  GET /api/ops/status  interactive admins only. Versions, dependency health, document-queue counters (stuck/dead-letter),
                       overdue compliance work, audit-chain head (full verification with ?verify_audit=true) and
                       configuration warnings. Never returns secrets, only whether they are set.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from api.auth import require_interactive_admin
from lambda_erp import database as dbmod
from lambda_erp.database import get_db
from msme.compliance import audit
from msme.documents.service import MAX_ATTEMPTS, STALE_PROCESSING
from msme.documents.storage import get_storage
from msme.ops.scheduler import interval_from_env

router = APIRouter(tags=["ops"])
_admin = Depends(require_interactive_admin)
READY_TTL_SECONDS = 5.0
_cache: dict = {"at": 0.0, "checks": None}


def clear_cache() -> None:
    _cache.update(at=0.0, checks=None)


def _check_database() -> tuple[bool, str]:
    get_db().sql("SELECT 1")
    return True, "ok"


def _check_migrations() -> tuple[bool, str]:
    head = dbmod.Database.MIGRATIONS[-1][0]
    current = get_db().sql('SELECT MAX(version) AS v FROM "_SchemaMigrations"')[0]["v"]
    return (current == head), f"schema version {current} of {head}"


def _check_storage() -> tuple[bool, str]:
    return get_storage().healthcheck()


CHECKS = (("database", _check_database), ("migrations", _check_migrations), ("storage", _check_storage))


def run_checks(force: bool = False) -> list[dict]:
    """Each check is isolated: one failing dependency never hides the others. Cached for a few seconds so public
    polling cannot hammer the database or the object store."""
    now = time.monotonic()
    if not force and _cache["checks"] is not None and now - _cache["at"] < READY_TTL_SECONDS:
        return _cache["checks"]
    results = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}"
        results.append({"name": name, "ok": bool(ok), "detail": detail})
    _cache.update(at=now, checks=results)
    return results


@router.get("/ready")
def ready():
    failed = [c["name"] for c in run_checks() if not c["ok"]]
    if failed:
        return JSONResponse({"status": "not_ready", "failed": failed}, status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse({"status": "ready"}, headers={"Cache-Control": "no-store"})


def _version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def build_status(verify_audit: bool = False) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    stale_before = (now - STALE_PROCESSING).isoformat(timespec="seconds")
    by_status = {r["status"]: r["n"] for r in db.sql('SELECT status, COUNT(*) AS n FROM "MSME Document" GROUP BY status')}
    stuck = db.sql('SELECT COUNT(*) AS n FROM "MSME Document" WHERE status = \'processing\' AND updated_at < ?', [stale_before])[0]["n"]
    dead = db.sql('SELECT COUNT(*) AS n FROM "MSME Document" WHERE status = \'failed\' AND attempts >= ?', [MAX_ATTEMPTS])[0]["n"]
    oldest = db.sql('SELECT MIN(created_at) AS t FROM "MSME Document" WHERE status = \'uploaded\'')[0]["t"]
    queue_age = None
    if oldest:
        try:
            queue_age = int((now - datetime.fromisoformat(oldest)).total_seconds())
        except ValueError:
            queue_age = None
    today = date.today().isoformat()
    overdue = db.sql('SELECT COUNT(*) AS n FROM "MSME Compliance Task" WHERE status <> \'filing_recorded\' AND due_date < ? AND NOT (optional = 1 AND status = \'open\')', [today])[0]["n"]
    in_review = db.sql('SELECT COUNT(*) AS n FROM "MSME Workpaper" WHERE status = \'submitted\'')[0]["n"]
    storage = get_storage()
    storage_ok, storage_detail = storage.healthcheck()
    chain = audit.verify(db) if verify_audit else None

    warnings = []

    def warn(code, message):
        warnings.append({"code": code, "message": message})

    if db.dialect == "sqlite":
        warn("sqlite_database", "The database is SQLite; use PostgreSQL for production.")
    if getattr(storage, "backend", "") == "local" and getattr(storage, "is_default_location", False):
        warn("ephemeral_document_storage", "MSME_STORAGE_DIR is not set: documents are on an ephemeral default path and will not survive a redeploy.")
    if not os.environ.get("DOCKET_TEXT_MODEL"):
        warn("extraction_not_configured", "DOCKET_TEXT_MODEL is not set: document extraction will fail until a model is configured.")
    if not shutil.which("tesseract"):
        warn("ocr_engine_missing", "The Tesseract OCR binary was not found: scanned documents cannot be read.")
    if not os.environ.get("JWT_SECRET_KEY"):
        warn("jwt_secret_from_disk", "JWT_SECRET_KEY is not set: the signing secret is generated on local disk.")
    if os.environ.get("MSME_REQUIRE_DISTINCT_APPROVER", "1").strip() in ("0", "false", "no"):
        warn("maker_checker_waived", "Maker-checker is waived by policy: preparers can approve their own workpapers.")
    if os.environ.get("LAMBDA_ERP_AUTO_DEMO") == "1":
        warn("demo_mode", "LAMBDA_ERP_AUTO_DEMO=1 seeds demo data; do not use for real books.")
    if interval_from_env() == 0:
        warn("sweep_disabled", "MSME_SWEEP_INTERVAL_SECONDS is not set: queued documents are processed only when triggered.")
    if stuck:
        warn("stuck_documents", f"{stuck} document(s) have been 'processing' for over {int(STALE_PROCESSING.total_seconds() // 60)} minutes.")
    if dead:
        warn("dead_letter_documents", f"{dead} document(s) exhausted automatic retries and need a manager to reprocess.")
    if chain is not None and not chain["ok"]:
        warn("audit_chain_broken", f"Audit chain verification failed at event {chain['first_bad_seq']}: {chain['reason']}.")

    return {
        "app": {"version": _version("lambda-erp"), "python": sys.version.split()[0], "docket": _version("docket-idp")},
        "readiness": run_checks(force=True),
        "database": {"dialect": db.dialect, "schema_version": db.sql('SELECT MAX(version) AS v FROM "_SchemaMigrations"')[0]["v"],
                     "schema_head": dbmod.Database.MIGRATIONS[-1][0]},
        "storage": {"backend": getattr(storage, "backend", "unknown"), "healthy": storage_ok, "detail": storage_detail},
        "documents": {"by_status": by_status, "stuck_processing": stuck, "dead_letter": dead, "oldest_queued_age_seconds": queue_age},
        "compliance": {"overdue_tasks": overdue, "workpapers_in_review": in_review},
        "audit": {"head": audit.head(db), "verified": chain},
        "background": {"sweep_interval_seconds": interval_from_env()},
        "warnings": warnings,
    }


@router.get("/ops/status")
def status(verify_audit: bool = Query(default=False), _user: dict = _admin):
    return build_status(verify_audit)
