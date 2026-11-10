#!/usr/bin/env python3
"""Operations: structured logging, request ids, safe 500s, readiness, admin status, scheduled sweep.

Run:  python -m tests.test_msme_ops      (LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True


def main() -> int:
    warnings.filterwarnings("ignore")
    from tests._msme_db import assert_backend, reset_test_db

    db_path = reset_test_db("lambda_msme_ops_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_ops_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-ops-jwt-secret-value", "OPENAI_API_KEY": "sk-SYNTHETICOPSKEY1234567890",
                       "MSME_STORAGE_DIR": storage_dir})
    os.environ.pop("MSME_SWEEP_INTERVAL_SECONDS", None)
    os.environ.pop("DOCKET_TEXT_MODEL", None)

    from fastapi.testclient import TestClient
    from api.main import app
    from api.observability import JsonFormatter, configure_logging, redact, request_id_var
    from api.routers import ops
    from lambda_erp import database as dbmod
    from lambda_erp.database import get_db
    from msme.compliance.service import ComplianceService
    from msme.ops.scheduler import PeriodicSweeper, interval_from_env

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    # ---------------- pure: redaction and formatting ----------------
    leaked = "postgresql://lambda:hunter2pw@db.internal/x Bearer abc.DEF-123 sk-ABCDEFGH12345678 password=swordfish token: t0k3n api_key=AKIA1234 sig=SECRETSIG"
    out = redact(leaked)
    check("redaction removes URL passwords, bearer tokens, API keys and key=value credentials",
          not any(x in out for x in ("hunter2pw", "abc.DEF", "ABCDEFGH12345678", "swordfish", "t0k3n", "AKIA1234", "SECRETSIG")) and "db.internal" in out, out)
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom %s", ("password=hunter2",), None)
    rec.event, rec.status = "x", 500
    token = request_id_var.set("req-12345678")
    try:
        line = json.loads(JsonFormatter().format(rec))
    finally:
        request_id_var.reset(token)
    check("log lines are valid JSON with level, logger, request id and extras",
          line["level"] == "ERROR" and line["request_id"] == "req-12345678" and line["status"] == 500 and "hunter2" not in line["message"] and line["ts"].endswith("+00:00"), str(line))
    try:
        raise ValueError("connect to postgresql://u:pw123@h/db failed " + "x" * 6000)
    except ValueError:
        import sys as _sys
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), _sys.exc_info())
    line = json.loads(JsonFormatter().format(rec))
    check("stack traces are redacted and truncated", "pw123" not in line["stack"] and len(line["stack"]) <= 4000 and line["error_type"] == "ValueError")

    # ---------------- capture logs ----------------
    captured: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(json.loads(JsonFormatter().format(record)))

    handler = Capture(level=logging.DEBUG)
    logging.getLogger().addHandler(handler)
    configure_logging("INFO")
    n_handlers = len(logging.getLogger().handlers)
    configure_logging("INFO")
    check("configure_logging is idempotent (no duplicate handlers)", len(logging.getLogger().handlers) == n_handlers)

    def requests_logged():
        return [c for c in captured if c.get("event") == "request"]

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert_backend(bool(os.environ.get("LAMBDA_ERP_TEST_DB")))
        assert admin.post("/api/auth/register", json={"email": "a@x.invalid", "full_name": "A", "password": "synthetic-pass-1"}).status_code == 200
        admin.put("/api/auth/settings", json={"allow_public_signup": "1"})
        viewer = TestClient(app, raise_server_exceptions=False)
        assert viewer.post("/api/auth/register", json={"email": "v@x.invalid", "full_name": "V", "password": "synthetic-pass-2"}).status_code == 200
        tok = admin.post("/api/auth/invite", json={"email": "m@x.invalid", "role": "manager"}).json()["token"]
        mgr = TestClient(app, raise_server_exceptions=False)
        assert mgr.post("/api/auth/register", json={"email": "m@x.invalid", "full_name": "M", "password": "synthetic-pass-3", "invite_token": tok}).status_code == 200
        anon = TestClient(app, raise_server_exceptions=False)

        # ---------------- request ids ----------------
        r = anon.get("/api/auth/setup-status")
        rid = r.headers.get("x-request-id", "")
        check("every response carries an X-Request-ID", len(rid) == 32 and all(c in "0123456789abcdef" for c in rid), rid)
        r = anon.get("/api/auth/setup-status", headers={"X-Request-ID": "client-supplied-id-001"})
        check("a safe client-supplied id is honoured and echoed", r.headers["x-request-id"] == "client-supplied-id-001")
        for bad in ("short", "has spaces in it 123", "semi;colon;injection123", "x" * 65, "<script>alert(1)</script>"):
            r = anon.get("/api/auth/setup-status", headers={"X-Request-ID": bad})
            if r.headers["x-request-id"] == bad:
                check(f"unsafe request id {bad!r} is replaced", False)
                break
        else:
            check("unsafe, too-short or too-long client ids are replaced with a generated one", True)

        # ---------------- the request log line ----------------
        captured.clear()
        anon.get("/api/auth/setup-status?token=QUERYSECRET987&email=leak@x.invalid", headers={"Authorization": "Bearer HEADERSECRET654", "Cookie": "lambda_erp_token=COOKIESECRET321"})
        line = requests_logged()[-1]
        blob = json.dumps(captured)
        check("one structured request line: method, route, status, duration, request id",
              line["method"] == "GET" and line["path"] == "/api/auth/setup-status" and line["status"] == 200 and line["duration_ms"] >= 0 and len(line["request_id"]) >= 8, str(line))
        check("query strings, bearer tokens and cookies never reach the logs", not any(x in blob for x in ("QUERYSECRET987", "leak@x.invalid", "HEADERSECRET654", "COOKIESECRET321")), blob[:300])
        captured.clear()
        r = anon.get("/api/auth/setup-status", headers={"X-Request-ID": "correlate-me-0001"})
        check("the logged request id matches the response header", requests_logged()[-1]["request_id"] == "correlate-me-0001")
        captured.clear()
        anon.get("/api/health"); anon.get("/api/ready")
        check("health and readiness polls are not logged at INFO (no log flood)", not requests_logged())
        captured.clear()
        viewer.get("/api/msme/documents/MDOC-0123456789ab")
        line = requests_logged()[-1]
        check("record ids are replaced by the route template in the request log",
              line["path"] == "/api/msme/documents/{document_id}" and line["status"] == 404 and "MDOC-0123456789ab" not in json.dumps(captured), str(line))
        captured.clear()
        viewer.post("/api/msme/documents", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
        check("a 403 is logged at INFO with its status", requests_logged()[-1]["status"] == 403 and requests_logged()[-1]["level"] == "INFO")

        # ---------------- unhandled exceptions ----------------
        def boom():
            raise RuntimeError("db failed: postgresql://lambda:hunter2pw@db.internal/x password=swordfish")

        app.add_api_route("/api/_boom_test", boom, methods=["GET"])
        app.router.routes.insert(0, app.router.routes.pop())
        captured.clear()
        r = anon.get("/api/_boom_test", headers={"X-Request-ID": "crash-test-id-0001"})
        errors = [c for c in captured if c["level"] == "ERROR"]
        check("an unhandled exception returns a generic JSON 500 carrying the request id",
              r.status_code == 500 and r.json() == {"detail": "Internal server error", "request_id": "crash-test-id-0001"} and r.headers["x-request-id"] == "crash-test-id-0001", r.text)
        check("the 500 response does not leak the exception text", "hunter2pw" not in r.text and "swordfish" not in r.text and "RuntimeError" not in r.text)
        check("the stack trace is logged at ERROR with the request id, secrets redacted",
              any(e.get("stack") and "RuntimeError" in e["stack"] and e["request_id"] == "crash-test-id-0001" for e in errors)
              and "hunter2pw" not in json.dumps(captured) and "swordfish" not in json.dumps(captured))
        check("the failed request is logged with status 500 at ERROR", any(c.get("event") == "request" and c["status"] == 500 and c["level"] == "ERROR" for c in captured))

        # ---------------- readiness ----------------
        ops.clear_cache()
        r = anon.get("/api/ready")
        check("ready: 200 with a minimal body", r.status_code == 200 and r.json() == {"status": "ready"} and r.headers["cache-control"] == "no-store", r.text)
        original = ops.CHECKS

        def with_failing(name):
            fake = lambda: (_ for _ in ()).throw(ConnectionError("pg at 10.0.0.5 password=swordfish"))  # noqa: E731
            return tuple((n, fake if n == name else f) for n, f in original)

        for name in ("database", "storage"):
            ops.CHECKS = with_failing(name)
            ops.clear_cache()
            r = anon.get("/api/ready")
            check(f"{name} failure -> 503 naming only the failing check, no details", r.status_code == 503 and r.json() == {"status": "not_ready", "failed": [name]}
                  and "10.0.0.5" not in r.text and "swordfish" not in r.text, r.text)
        ops.CHECKS = original
        real_migrations = list(dbmod.Database.MIGRATIONS)
        dbmod.Database.MIGRATIONS = [*real_migrations, (9999, "pending", None)]
        ops.clear_cache()
        r = anon.get("/api/ready")
        dbmod.Database.MIGRATIONS = real_migrations
        check("a database behind the code's schema version is not ready", r.status_code == 503 and r.json()["failed"] == ["migrations"], r.text)
        ops.CHECKS = tuple((n, (lambda: (_ for _ in ()).throw(OSError("x"))) if n in ("database", "storage") else f) for n, f in original)
        ops.clear_cache()
        check("several failures are all reported", anon.get("/api/ready").json()["failed"] == ["database", "storage"])
        calls = []
        ops.CHECKS = (("storage", lambda: (calls.append(1), (True, "ok"))[1]),)
        ops.clear_cache()
        anon.get("/api/ready"); anon.get("/api/ready"); anon.get("/api/ready")
        check("public polling is cached for a few seconds (the dependency is probed once)", len(calls) == 1, str(len(calls)))
        ops.run_checks(force=True)
        check("force bypasses the cache", len(calls) == 2)
        ops.CHECKS = original
        ops.clear_cache()
        check("recovery: ready again once the dependency is healthy", anon.get("/api/ready").status_code == 200)

        # ---------------- admin status ----------------
        check("status: anonymous 401, viewer 403, manager 403",
              anon.get("/api/ops/status").status_code == 401 and viewer.get("/api/ops/status").status_code == 403 and mgr.get("/api/ops/status").status_code == 403)
        db = get_db()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        now_s = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for i, (status, attempts, updated) in enumerate((("processing", 1, old), ("failed", 3, now_s), ("uploaded", 0, now_s), ("approved", 1, now_s))):
            db.insert("MSME Document", {"id": f"MDOC-00000000000{i}", "filename": "f.pdf", "mime_type": "application/pdf", "size_bytes": 10, "sha256": f"{i:064d}",
                                        "storage_key": f"00/{i:064d}.pdf", "status": status, "attempts": attempts, "created_at": old, "updated_at": updated})
        db.commit()
        ComplianceService().set_profile  # noqa: B018  (ensure the module imports cleanly)
        s = admin.get("/api/ops/status").json()
        check("status: versions, schema version at head, dialect and storage backend",
              s["database"]["schema_version"] == s["database"]["schema_head"] and s["database"]["dialect"] in ("sqlite", "postgres")
              and s["storage"]["backend"] == "local" and s["storage"]["healthy"] and s["app"]["version"] and s["app"]["python"])
        check("status: document queue counters (stuck, dead-letter, oldest queued age)",
              s["documents"]["by_status"] == {"processing": 1, "failed": 1, "uploaded": 1, "approved": 1} and s["documents"]["stuck_processing"] == 1
              and s["documents"]["dead_letter"] == 1 and s["documents"]["oldest_queued_age_seconds"] >= 7000, str(s["documents"]))
        codes = {w["code"] for w in s["warnings"]}
        check("status: warnings for stuck documents, dead letters, missing extraction model and a disabled sweep",
              {"stuck_documents", "dead_letter_documents", "extraction_not_configured", "sweep_disabled"} <= codes, str(codes))
        text = json.dumps(s)
        check("status never returns secrets (only whether they are set)", not any(x in text for x in ("synthetic-ops-jwt-secret-value", "SYNTHETICOPSKEY", db_path if "://" in db_path else "postgresql://")))
        check("status without ?verify_audit does not scan the chain", s["audit"]["verified"] is None)
        ComplianceService().set_profile  # (audit events come from the compliance service)
        db.insert("Company", {"name": "Ops Co", "company_name": "Ops Co", "default_currency": "INR"}); db.commit()
        ComplianceService().set_profile("Ops Co", "regular_monthly", None, None, "alice")
        v = admin.get("/api/ops/status?verify_audit=true").json()
        check("verify_audit=true verifies the chain", v["audit"]["verified"]["ok"] and v["audit"]["head"]["seq"] >= 1 and "audit_chain_broken" not in {w["code"] for w in v["warnings"]})
        ev = db.sql('SELECT id, detail FROM "MSME Audit Event" ORDER BY seq LIMIT 1')[0]
        db.sql('UPDATE "MSME Audit Event" SET detail = ? WHERE id = ?', ['{"forged":1}', ev["id"]]); db.commit()
        v = admin.get("/api/ops/status?verify_audit=true").json()
        check("a tampered audit chain is surfaced as a warning on the status page", not v["audit"]["verified"]["ok"] and "audit_chain_broken" in {w["code"] for w in v["warnings"]})
        db.sql('UPDATE "MSME Audit Event" SET detail = ? WHERE id = ?', [ev["detail"], ev["id"]]); db.commit()

        # warnings are driven by configuration
        os.environ.pop("MSME_STORAGE_DIR")
        os.environ["MSME_REQUIRE_DISTINCT_APPROVER"] = "0"
        os.environ["DOCKET_TEXT_MODEL"] = "m"
        codes = {w["code"] for w in admin.get("/api/ops/status").json()["warnings"]}
        os.environ["MSME_STORAGE_DIR"] = storage_dir
        os.environ.pop("MSME_REQUIRE_DISTINCT_APPROVER"); os.environ.pop("DOCKET_TEXT_MODEL")
        check("config warnings: ephemeral document storage and waived maker-checker appear; a configured model clears its warning",
              {"ephemeral_document_storage", "maker_checker_waived"} <= codes and "extraction_not_configured" not in codes, str(codes))

        # ---------------- the app starts and stops cleanly with the sweeper enabled ----------------
        os.environ["MSME_SWEEP_INTERVAL_SECONDS"] = "30"
        try:
            with TestClient(app) as c2:
                check("app lifespan starts and stops cleanly with the sweeper enabled", c2.get("/api/health").status_code == 200)
        finally:
            os.environ.pop("MSME_SWEEP_INTERVAL_SECONDS")

    # ---------------- scheduler ----------------
    check("interval parsing: unset/0/garbage disable; small values clamp to 30s; large clamp to a day",
          [interval_from_env({}), interval_from_env({"MSME_SWEEP_INTERVAL_SECONDS": "0"}), interval_from_env({"MSME_SWEEP_INTERVAL_SECONDS": "abc"}),
           interval_from_env({"MSME_SWEEP_INTERVAL_SECONDS": "-5"}), interval_from_env({"MSME_SWEEP_INTERVAL_SECONDS": "5"}),
           interval_from_env({"MSME_SWEEP_INTERVAL_SECONDS": "300"}), interval_from_env({"MSME_SWEEP_INTERVAL_SECONDS": "999999"})] == [0.0, 0.0, 0.0, 0.0, 30.0, 300.0, 86400.0])

    async def scheduler_checks():
        runs = []
        sweeper = PeriodicSweeper(lambda: (runs.append(1), {"processed": 1, "failed": 0})[1], 0.05, first_delay=0.01)
        sweeper.start()
        await asyncio.sleep(0.4)
        await sweeper.stop()
        stopped_at = len(runs)
        await asyncio.sleep(0.2)
        a = sweeper.ticks >= 3 and len(runs) == stopped_at and sweeper._task is None
        n = []

        def flaky():
            n.append(1)
            if len(n) == 1:
                raise RuntimeError("transient failure postgresql://u:pw@h/db")
            return {"processed": 0, "failed": 0}

        s2 = PeriodicSweeper(flaky, 0.05, first_delay=0.01)
        s2.start()
        await asyncio.sleep(0.4)
        await s2.stop()
        b = s2.failures == 1 and s2.ticks >= 2
        s3 = PeriodicSweeper(lambda: {}, 0)
        s3.start()
        c = s3._task is None
        return a, b, c

    a, b, c = asyncio.run(scheduler_checks())
    check("the sweeper ticks repeatedly and stops cleanly (no ticks after stop)", a)
    check("a failing tick is logged and the loop keeps running", b)
    check("an interval of 0 starts nothing", c)
    check("scheduler failures are logged at ERROR with a redacted stack",
          any(c.get("event") == "sweep_failed" and c["level"] == "ERROR" and "pw@" not in c.get("stack", "") for c in captured))

    logging.getLogger().removeHandler(handler)
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
