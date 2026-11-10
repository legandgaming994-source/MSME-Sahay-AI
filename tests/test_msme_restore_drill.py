#!/usr/bin/env python3
"""Backup and restore drill (SQLite always; PostgreSQL when LAMBDA_ERP_TEST_DB is set and pg_dump/pg_restore exist).

State is built through the real services (documents, compliance, credit), backed up (database + document files),
the original is DESTROYED, a fresh database is restored and the system is re-pointed at it. The restored system must be
identical to the backed-up one (row counts, schema version, indexes, audit hash chain, every original document checksum),
must NOT contain changes made after the backup (it is a point-in-time restore, not a copy of the live state), and must
detect tampering with the restored audit log.

Run:  python -m tests.test_msme_restore_drill      (LAMBDA_ERP_TEST_DB=postgresql://... adds PostgreSQL)
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True


def main() -> int:
    postgres_url = os.environ.get("LAMBDA_ERP_TEST_DB")
    use_pg = bool(postgres_url) and shutil.which("pg_dump") and shutil.which("pg_restore")
    if postgres_url and not use_pg:
        if os.environ.get("REQUIRE_PG_TOOLS") == "1":
            print("FAIL pg_dump/pg_restore are required (REQUIRE_PG_TOOLS=1)")
            return 1
        print("SKIP PostgreSQL drill (pg_dump/pg_restore not installed)")
    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    for backend in (["sqlite"] + (["postgres"] if use_pg else [])):
        tag = f"[{backend}] "
        work = Path(tempfile.mkdtemp(prefix="msme_restore_drill_"))
        storage_dir = work / "documents"
        if backend == "sqlite":
            live = str(work / "live.db")
        else:
            from tests._msme_db import reset_test_db
            live = reset_test_db("drill_")
        os.environ.update({"LAMBDA_ERP_DB": live, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "", "JWT_SECRET_KEY": "synthetic-drill",
                           "MSME_STORAGE_DIR": str(storage_dir)})
        from lambda_erp.database import get_db, setup
        setup(live)
        from msme.compliance import audit
        from msme.compliance.service import ComplianceService
        from msme.credit.service import CreditService
        from msme.documents.extraction import ExtractedField, ExtractionOutcome
        from msme.documents.service import DocumentService
        from msme.documents.storage import LocalDocumentStorage

        db = get_db()
        db.insert("Company", {"name": "Drill Co", "company_name": "Drill Co", "default_currency": "INR"}); db.commit()
        comp, credit = ComplianceService(), CreditService()
        comp.set_profile("Drill Co", "regular_monthly", None, "27AAPFU0939F1ZV", "alice")
        comp.generate_tasks("Drill Co", 2026, "alice")
        credit.set_fact("Drill Co", "sector", "service", "alice")
        credit.set_fact("Drill Co", "turnover_inr", 5_000_000, "alice")
        credit.assess("Drill Co", "alice")
        vals = {}

        class Fake:
            name, version = "fake", "1"

            def extract(self, path):
                return ExtractionOutcome("extracted", "invoice", [ExtractedField(k, v, .9, 1, v, None, "verified") for k, v in vals.items()], extractor="fake", extractor_version="1")

        docs = DocumentService(Fake(), LocalDocumentStorage(storage_dir))
        doc_ids = []
        for i in range(3):
            vals.clear(); vals.update({"invoice_number": f"DR-{i}", "issue_date": "2026-06-10", "seller.name": "S", "subtotal": "100.00", "tax_amount": "18.00", "total_amount": "118.00"})
            d, _ = docs.ingest("u", f"d{i}.pdf", f"%PDF-1.4\n% drill {backend} {i}\n%%EOF\n".encode(), "application/pdf")
            docs.process(d["id"]); docs.approve(d["id"], "reviewer"); doc_ids.append(d["id"])

        def fingerprint():
            d = get_db()
            tables = [r[0] for r in d.sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'MSME %' ORDER BY name" if d.dialect == "sqlite"
                else "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'MSME %' ORDER BY 1", as_dict=False)]
            counts = {t: d.sql(f'SELECT COUNT(*) AS n FROM "{t}"')[0]["n"] for t in tables}
            idx = sorted(r[0] for r in d.sql("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_msme_%'" if d.dialect == "sqlite"
                                              else "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'idx_msme_%'", as_dict=False))
            chain = audit.verify(d)
            return {"counts": counts, "indexes": idx, "schema": d.sql('SELECT MAX(version) AS v FROM "_SchemaMigrations"')[0]["v"],
                    "audit_ok": chain["ok"], "audit_events": chain["events"], "audit_head": audit.head(d)}

        before = fingerprint()
        check(tag + "the system under backup is healthy and non-trivial (all MSME tables present, audit chain valid)",
              before["audit_ok"] and before["audit_events"] == 5 and len(before["counts"]) >= 12 and before["counts"]["MSME Document"] == 3 and len(before["indexes"]) >= 10, str(before["counts"]))

        # ---------------- backup: database + document files ----------------
        t0 = time.perf_counter()
        if backend == "sqlite":
            dump = work / "backup.db"
            src = sqlite3.connect(live); dst = sqlite3.connect(dump); src.backup(dst); dst.close(); src.close()
        else:
            dump = work / "backup.dump"
            subprocess.run(["pg_dump", "--format=custom", "--no-owner", f"--file={dump}", live], check=True, capture_output=True)
        backup_dir = work / "documents-backup"
        shutil.copytree(storage_dir, backup_dir)
        backup_seconds = time.perf_counter() - t0

        # ---------------- later activity (must NOT be in the restored system) then DISASTER ----------------
        credit.set_fact("Drill Co", "credit_facility_amount", 123456, "alice")
        later_events = audit.head(get_db())["seq"]
        later_doc, _ = docs.ingest("u", "later.pdf", f"%PDF-1.4\n% LATER {backend}\n%%EOF\n".encode(), "application/pdf")
        shutil.rmtree(storage_dir)
        if backend == "sqlite":
            get_db().conn.close()
            os.remove(live)
        else:
            import psycopg
            get_db().conn.close()
            with psycopg.connect(live, autocommit=True) as c:
                c.execute("DROP SCHEMA public CASCADE"); c.execute("CREATE SCHEMA public")

        # ---------------- restore into a fresh database and re-point the app ----------------
        t1 = time.perf_counter()
        if backend == "sqlite":
            restored = str(work / "restored.db")
            shutil.copy(dump, restored)
        else:
            import psycopg
            restored_url = live.rsplit("/", 1)[0] + "/lambda_restore_drill"
            with psycopg.connect(live.rsplit("/", 1)[0] + "/postgres", autocommit=True) as c:
                c.execute("DROP DATABASE IF EXISTS lambda_restore_drill"); c.execute("CREATE DATABASE lambda_restore_drill")
            subprocess.run(["pg_restore", "--no-owner", f"--dbname={restored_url}", str(dump)], check=True, capture_output=True)
            restored = restored_url
        shutil.copytree(backup_dir, storage_dir)
        os.environ["LAMBDA_ERP_DB"] = restored
        setup(restored)
        restore_seconds = time.perf_counter() - t1

        after = fingerprint()
        check(tag + "restored table row counts, schema version and indexes are identical to the backup", after["counts"] == before["counts"] and after["schema"] == before["schema"] and after["indexes"] == before["indexes"], f"{after['counts']} vs {before['counts']}")
        check(tag + "the restored audit hash chain verifies and its head is identical (no event lost, none altered)",
              after["audit_ok"] and after["audit_head"] == before["audit_head"] and after["audit_events"] == before["audit_events"], str(after["audit_head"]))
        check(tag + "it is a point-in-time restore: activity after the backup is absent",
              not get_db().sql('SELECT id FROM "MSME Document" WHERE id = ?', [later_doc["id"]]) and audit.head(get_db())["seq"] < later_events
              and not [f for f in credit.get_facts("Drill Co")["facts"] if f["key"] == "credit_facility_amount"])
        restored_docs = DocumentService(Fake(), LocalDocumentStorage(storage_dir))
        ok = True
        for doc_id in doc_ids:
            try:
                data, mime, _ = restored_docs.read_original(doc_id)     # verifies SHA-256 against the restored database
                ok = ok and mime == "application/pdf" and data.startswith(b"%PDF-")
            except Exception as exc:  # noqa: BLE001
                ok = False
        check(tag + "every approved document's original is restored from the file backup and passes its checksum", ok)
        check(tag + "the restored application keeps working (new audited writes append to the restored chain)",
              credit.set_fact("Drill Co", "credit_facility_amount", 999, "bob")["value"] == 999 and audit.verify(get_db())["ok"] and audit.head(get_db())["seq"] == before["audit_head"]["seq"] + 1)
        ev = get_db().sql('SELECT id, detail FROM "MSME Audit Event" ORDER BY seq LIMIT 1')[0]
        get_db().sql('UPDATE "MSME Audit Event" SET detail = ? WHERE id = ?', ['{"tampered":true}', ev["id"]]); get_db().commit()
        check(tag + "tampering with the restored audit log is detected (the chain is usable as restore verification)", not audit.verify(get_db())["ok"])
        # a backup taken while the file backup is incomplete is detected, not silently accepted
        os.remove(next((storage_dir.rglob("*.pdf"))))
        try:
            broken = sum(1 for i in doc_ids if _unreadable(restored_docs, i))
        except Exception:  # noqa: BLE001
            broken = -1
        check(tag + "a missing document file is detected on read (incomplete file backups are not silent)", broken == 1, str(broken))
        print(f"INFO  {backend}: backup {backup_seconds:.2f}s, restore {restore_seconds:.2f}s for {sum(before['counts'].values())} rows and {len(doc_ids)} documents")
        get_db().conn.close()
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


def _unreadable(service, doc_id) -> bool:
    try:
        service.read_original(doc_id)
        return False
    except Exception:  # noqa: BLE001
        return True


if __name__ == "__main__":
    sys.exit(main())
