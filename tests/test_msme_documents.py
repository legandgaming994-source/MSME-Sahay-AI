#!/usr/bin/env python3
"""D-1/D-2/D-3 service behaviour with a scripted extractor (no network, no OCR).

Real database, real storage on disk, real concurrency. Covers: hostile uploads, content-hash dedupe
(including a threaded race), exactly-one-processor claiming, retry exhaustion, stale-job recovery,
error scrubbing, correction history, approval gating, reprocess preservation, business-key duplicate
detection, tamper detection and the sweep.

Run:  python -m tests.test_msme_documents
"""

import os
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True


def main() -> int:
    from tests._msme_db import reset_test_db

    db_path = reset_test_db("lambda_msme_docs_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-docs"})

    from lambda_erp.database import get_db, setup

    setup(db_path)  # a bare get_db() would silently use an in-memory SQLite
    from tests._msme_db import assert_backend

    assert_backend(bool(os.environ.get('LAMBDA_ERP_TEST_DB')))
    from msme.documents.extraction import ExtractedField, ExtractionIssue, ExtractionOutcome
    from msme.documents.service import MAX_ATTEMPTS, MAX_BYTES, DocumentError, DocumentService
    from msme.documents.storage import LocalDocumentStorage

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    def raises(code, fn, *a, **k):
        try:
            fn(*a, **k)
        except DocumentError as exc:
            return exc.code == code
        return False

    counter = [0]

    def pdf(tag="") -> bytes:
        counter[0] += 1
        return f"%PDF-1.4\n% synthetic {counter[0]} {tag}\n1 0 obj\n<< >>\nendobj\n%%EOF\n".encode()

    BASE = {
        "invoice_number": "INV-0042", "issue_date": "2026-09-01", "seller.name": "Northgate Supplies",
        "seller.tax_ids[0].value": "27AAPFU0939F1ZV", "seller.tax_ids[0].scheme": "gst",
        "line_items[0].quantity": "10", "line_items[0].unit_price": "100", "line_items[0].total": "1000",
        "subtotal": "1000", "tax_rate_percent": "18", "tax_amount": "180", "total_amount": "1180",
    }

    def outcome(values=None, status="extracted", issues=(), conf=0.95):
        if values is None:
            counter[0] += 1
            values = dict(BASE, invoice_number=f"INV-AUTO-{counter[0]}")
        fields = [ExtractedField(p, v, conf, 1, v, {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2}, "verified")
                  for p, v in values.items()]
        return ExtractionOutcome(status, "invoice", fields, list(issues), extractor="fake", extractor_version="1")

    class Fake:
        name, version = "fake", "1"

        def __init__(self, script=None, delay=0.0):
            self.script, self.delay, self.calls, self.lock = script, delay, 0, threading.Lock()

        def extract(self, path):
            with self.lock:
                self.calls += 1
                n = self.calls
            time.sleep(self.delay)
            step = self.script(n, path) if callable(self.script) else self.script
            if isinstance(step, Exception):
                raise step
            return step if step is not None else outcome()

    def svc(extractor=None, clock=None):
        return DocumentService(extractor or Fake(), LocalDocumentStorage(storage_dir), clock)

    db = get_db()

    # ---------------- hostile / invalid uploads ----------------
    s = svc()
    check("empty file rejected", raises("empty_file", s.ingest, "u", "a.pdf", b"", "application/pdf"))
    check("html declared as pdf rejected", raises("invalid_file", s.ingest, "u", "a.pdf", b"<html><script>1</script>", "application/pdf"))
    check("executable declared as pdf rejected", raises("invalid_file", s.ingest, "u", "a.pdf", b"MZ\x90\x00" + b"\0" * 64, "application/pdf"))
    check("oversize rejected (413 path)", raises("file_too_large", s.ingest, "u", "a.pdf", b"%PDF-" + b"0" * MAX_BYTES, "application/pdf"))
    check("csv rejected as unsupported type", raises("unsupported_type", s.ingest, "u", "a.csv", b"a,b\n1,2\n", "text/csv"))
    check("no rows created by rejected uploads", db.sql('SELECT COUNT(*) AS n FROM "MSME Document"')[0]["n"] == 0)

    # ---------------- ingest + storage ----------------
    data = pdf()
    doc, dup = s.ingest("alice", "../../evil/inv.pdf", data, "application/pdf")
    check("new upload registered as uploaded", not dup and doc["status"] == "uploaded" and doc["uploaded_by"] == "alice")
    check("filename stripped to a basename", doc["filename"] == "inv.pdf", doc["filename"])
    check("storage key is not exposed", "storage_key" not in doc)
    key = db.sql('SELECT storage_key FROM "MSME Document" WHERE id = ?', [doc["id"]])[0]["storage_key"]
    stored = os.path.join(storage_dir, key)
    check("stored under content-addressed key with owner-only mode",
          os.path.exists(stored) and stat.S_IMODE(os.stat(stored).st_mode) == 0o600
          and stat.S_IMODE(os.stat(os.path.dirname(stored)).st_mode) == 0o700)

    # ---------------- duplicate detection (exact content) ----------------
    doc2, dup2 = s.ingest("bob", "renamed.pdf", data, "application/pdf")
    check("identical bytes return the same document", dup2 and doc2["id"] == doc["id"])
    check("only one row for identical bytes", db.sql('SELECT COUNT(*) AS n FROM "MSME Document" WHERE sha256 = ?', [doc["sha256"]])[0]["n"] == 1)

    racer, race_data, ids, errs = svc(), pdf("race"), [], []

    def race():
        try:
            ids.append(racer.ingest("t", "r.pdf", race_data, "application/pdf")[0]["id"])
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    threads = [threading.Thread(target=race) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    check("8 concurrent identical uploads -> exactly one document", not errs and len(set(ids)) == 1 and len(ids) == 8, f"{errs} {set(ids)}")

    # ---------------- process: success, exclusivity, idempotency ----------------
    ex = Fake(delay=0.3)
    s = svc(ex)
    d, _ = s.ingest("u", "p.pdf", pdf(), "application/pdf")
    results = []
    workers = [threading.Thread(target=lambda: results.append(s.process(d["id"]))) for _ in range(5)]
    [t.start() for t in workers]; [t.join() for t in workers]
    check("5 concurrent process calls run the extractor exactly once", ex.calls == 1, str(ex.calls))
    done = s.get_document(d["id"])
    check("clean invoice -> extracted with no issues", done["status"] == "extracted" and done["issues"] == [], str(done["issues"]))
    f = {x["field_path"]: x for x in done["fields"]}
    check("fields persisted with original = machine = current and evidence",
          f["total_amount"]["original_value"] == f["total_amount"]["current_value"] == "1180"
          and f["total_amount"]["page"] == 1 and f["total_amount"]["bbox"] and f["total_amount"]["confidence"] == 0.95)
    s.process(d["id"])
    check("re-processing a finished document is a no-op", ex.calls == 1)

    # ---------------- failures, retries, dead letter ----------------
    ex = Fake(script=ExtractionOutcome("failed", error_code="ocr_unavailable", error_message="no tesseract"))
    s = svc(ex)
    d, _ = s.ingest("u", "f.pdf", pdf(), "application/pdf")
    st = [s.process(d["id"])["attempts"] for _ in range(MAX_ATTEMPTS)]
    check("failed outcome retried up to MAX_ATTEMPTS", st == list(range(1, MAX_ATTEMPTS + 1)), str(st))
    g = s.get_document(d["id"])
    check("failure recorded with code", g["status"] == "failed" and g["error_code"] == "ocr_unavailable")
    check("automatic retries exhausted -> max_attempts", raises("max_attempts", s.process, d["id"]))
    ex.script = None
    check("force reprocess recovers a dead-lettered document", s.process(d["id"], force=True)["status"] == "extracted")

    ex = Fake(script=RuntimeError("upstream said Bearer abc.def.ghi key sk-ABCDEFGH12345 invalid"))
    s = svc(ex)
    d, _ = s.ingest("u", "x.pdf", pdf(), "application/pdf")
    g = s.process(d["id"])
    check("extractor exception -> failed, never stuck in processing", g["status"] == "failed" and g["error_code"] == "extractor_error")
    check("secrets scrubbed from stored error", "sk-ABCDEFGH" not in g["error_message"] and "abc.def" not in g["error_message"], g["error_message"])

    # ---------------- stale processing recovery ----------------
    t0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    ex = Fake()
    s = svc(ex, clock=lambda: t0)
    d, _ = s.ingest("u", "s.pdf", pdf(), "application/pdf")
    fresh = (t0 - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.sql('UPDATE "MSME Document" SET status = \'processing\', updated_at = ? WHERE id = ?', [fresh, d["id"]]); db.conn.commit()
    s.process(d["id"])
    check("a recently claimed job is not stolen", ex.calls == 0 and s.get_document(d["id"])["status"] == "processing")
    old = (t0 - timedelta(hours=1)).isoformat(timespec="seconds")
    db.sql('UPDATE "MSME Document" SET updated_at = ? WHERE id = ?', [old, d["id"]]); db.conn.commit()
    s.process(d["id"])
    check("a stale 'processing' job is reclaimed and finished", ex.calls == 1 and s.get_document(d["id"])["status"] == "extracted")

    # ---------------- review: confirmed issue blocks approval, corrections are audited ----------------
    bad = dict(BASE, total_amount="1500")  # 1000 + 180 != 1500
    s = svc(Fake(script=outcome(bad)))
    d, _ = s.ingest("u", "r.pdf", pdf(), "application/pdf")
    g = s.process(d["id"])
    check("arithmetic inconsistency is a CONFIRMED issue and needs review",
          g["status"] == "needs_review" and any(i["code"] == "TOTAL_MISMATCH" and i["severity"] == "CONFIRMED" for i in g["issues"]), str(g["issues"]))
    check("approval blocked while a confirmed issue is open", raises("confirmed_issues_open", s.approve, d["id"], "mgr"))
    check("correction needs a reason", raises("reason_required", s.correct_field, d["id"], "total_amount", "1180", "  ", "mgr"))
    check("invalid field path rejected", raises("invalid_field", s.correct_field, d["id"], "total amount;--", "1", "r", "mgr"))
    check("no-op correction rejected", raises("no_change", s.correct_field, d["id"], "total_amount", "1500", "r", "mgr"))
    g = s.correct_field(d["id"], "total_amount", "1180", "OCR misread 1180 as 1500", "mgr")
    f = {x["field_path"]: x for x in g["fields"]}
    check("correction updates current, preserves original, flags corrected",
          f["total_amount"]["current_value"] == "1180" and f["total_amount"]["original_value"] == "1500"
          and f["total_amount"]["machine_value"] == "1500" and f["total_amount"]["corrected"] is True)
    check("correction resolves the issue", not any(i["code"] == "TOTAL_MISMATCH" for i in g["issues"]))
    h = s.history(d["id"])
    check("history row records who, why, old and new",
          len(h) == 1 and h[0]["corrected_by"] == "mgr" and h[0]["old_value"] == "1500" and h[0]["new_value"] == "1180"
          and "misread" in h[0]["reason"])
    g = s.approve(d["id"], "mgr")
    check("approval succeeds once consistent", g["status"] == "approved" and g["approved_by"] == "mgr")
    g = s.correct_field(d["id"], "buyer.name", "Iberia Mantenimiento SA", "buyer was missing", "mgr2")
    check("editing an approved document revokes the approval", g["status"] == "needs_review" and g["approved_by"] is None)
    mf = {x["field_path"]: x for x in g["fields"]}["buyer.name"]
    check("correction can add a field the machine missed (original NULL, manual)",
          mf["original_value"] is None and mf["location_status"] == "manual" and mf["corrected"])
    check("history filter by field", len(s.history(d["id"], "buyer.name")) == 1 and len(s.history(d["id"])) == 2)

    # ---------------- acknowledgement: audited, never silent ----------------
    s = svc(Fake(script=outcome(dict(BASE, invoice_number="INV-ACK-1", total_amount="1500"))))
    d, _ = s.ingest("u", "ack.pdf", pdf(), "application/pdf")
    s.process(d["id"])
    check("acknowledging needs a reason", raises("reason_required", s.acknowledge_issues, d["id"], "TOTAL_MISMATCH", None, " ", "mgr"))
    check("acknowledging a non-existent issue -> issue_not_found", raises("issue_not_found", s.acknowledge_issues, d["id"], "NOPE", None, "r", "mgr"))
    g = s.acknowledge_issues(d["id"], "TOTAL_MISMATCH", "total_amount", "Supplier misprinted the total; accepted", "mgr")
    iss = [i for i in g["issues"] if i["code"] == "TOTAL_MISMATCH"][0]
    check("acknowledged issue stays visible with who/when/why",
          iss["acknowledged_by"] == "mgr" and iss["acknowledged_at"] and "misprinted" in iss["acknowledgement_reason"] and iss["severity"] == "CONFIRMED")
    g = s.correct_field(d["id"], "buyer.name", "Kurnool Traders", "added buyer", "mgr")
    iss = [i for i in g["issues"] if i["code"] == "TOTAL_MISMATCH"]
    check("acknowledgement survives an unrelated edit (identical issue)", iss and iss[0]["acknowledged_by"] == "mgr")
    check("acknowledged document can be approved", s.approve(d["id"], "mgr")["status"] == "approved")
    check("acknowledging an approved document is refused", raises("not_reviewable", s.acknowledge_issues, d["id"], "TOTAL_MISMATCH", None, "r", "mgr"))
    g = s.correct_field(d["id"], "total_amount", "1180", "fixed after all", "mgr")
    check("changing the values drops the stale acknowledgement with the issue", not [i for i in g["issues"] if i["code"] == "TOTAL_MISMATCH"])
    s2 = svc(Fake(script=outcome(dict(BASE, invoice_number="INV-ACK-2", total_amount="1500"))))
    d2, _ = s2.ingest("u", "ack2.pdf", pdf(), "application/pdf"); s2.process(d2["id"])
    s2.acknowledge_issues(d2["id"], "TOTAL_MISMATCH", None, "accepted", "mgr")
    g = s2.correct_field(d2["id"], "total_amount", "1600", "different wrong value", "mgr")
    mm = [i for i in g["issues"] if i["code"] == "TOTAL_MISMATCH"]
    check("a changed message (new numbers) is NOT covered by the old acknowledgement", mm and mm[0]["acknowledged_by"] is None, str(mm))

    # ---------------- reprocess preserves human work ----------------
    ex = Fake()
    s = svc(ex)
    first = dict(BASE, **{"seller.name": "Northgate Supplies", "buyer.name": "Old Buyer"})
    ex.script = outcome(first)
    d, _ = s.ingest("u", "rp.pdf", pdf(), "application/pdf")
    s.process(d["id"])
    s.correct_field(d["id"], "seller.name", "Northgate Supplies Pvt Ltd", "full legal name", "mgr")
    second = dict(BASE, **{"seller.name": "Northgate Suplies", "invoice_number": "INV-0042"})  # buyer.name vanished
    ex.script = outcome(second)
    g = s.process(d["id"], force=True)
    f = {x["field_path"]: x for x in g["fields"]}
    check("reprocess keeps the human value but records the new machine reading",
          f["seller.name"]["current_value"] == "Northgate Supplies Pvt Ltd" and f["seller.name"]["machine_value"] == "Northgate Suplies"
          and f["seller.name"]["original_value"] == "Northgate Supplies")
    check("reprocess drops uncorrected fields the machine no longer reads", "buyer.name" not in f)
    check("reprocess clears approval state and keeps history", g["status"] in ("extracted", "needs_review") and len(s.history(d["id"])) == 1)

    # ---------------- duplicate invoices (business key) ----------------
    DUP = dict(BASE, invoice_number="INV-DUP-7")
    ex = Fake(script=outcome(DUP))
    s = svc(ex)
    a, _ = s.ingest("u", "a.pdf", pdf(), "application/pdf"); ga = s.process(a["id"])
    check("first copy of an invoice is not a duplicate", ga["duplicate_of"] is None and ga["status"] == "extracted", str(ga["issues"]))
    b, _ = s.ingest("u", "b.pdf", pdf(), "application/pdf")
    ex.script = outcome(dict(DUP, invoice_number="inv dup-7", total_amount="1,180.00"))
    gb = s.process(b["id"])
    check("same supplier+number+date+total in different bytes -> duplicate candidate (heuristic)",
          gb["duplicate_of"] == a["id"]
          and any(i["code"] == "DUPLICATE_CANDIDATE" and i["severity"] == "HEURISTIC" for i in gb["issues"]), str(gb["issues"]))
    check("duplicate candidate forces review", gb["status"] == "needs_review")

    # ---------------- duplicate groups: the original is never flagged as a duplicate ----------------
    def dups(doc_ids):
        return {i: s.get_document(i)["duplicate_of"] for i in doc_ids}

    def has_dup_issue(i):
        return any(x["code"] == "DUPLICATE_CANDIDATE" for x in s.get_document(i)["issues"])

    GV = dict(BASE, invoice_number="INV-GROUP-1")
    ex = Fake(script=outcome(GV))
    s = svc(ex)
    ga, _ = s.ingest("u", "ga.pdf", pdf(), "application/pdf"); s.process(ga["id"]); s.approve(ga["id"], "r")
    gb, _ = s.ingest("u", "gb.pdf", pdf(), "application/pdf"); s.process(gb["id"])
    check("copy points at the original", dups([ga["id"], gb["id"]]) == {ga["id"]: None, gb["id"]: ga["id"]})
    s.correct_field(ga["id"], "buyer.name", "Kurnool Traders", "added buyer", "r")     # field NOT in the business key
    check("editing the ORIGINAL (non-key field) does not make it a duplicate of its own copy",
          dups([ga["id"], gb["id"]]) == {ga["id"]: None, gb["id"]: ga["id"]} and not has_dup_issue(ga["id"]), str(dups([ga["id"], gb["id"]])))
    s.correct_field(ga["id"], "invoice_number", "INV-GROUP-9", "renumbered", "r")      # field IN the business key
    check("when the original's key changes the copy becomes the original (flag and issue cleared)",
          dups([ga["id"], gb["id"]]) == {ga["id"]: None, gb["id"]: None} and not has_dup_issue(gb["id"]),
          str(dups([ga["id"], gb["id"]])))
    s.correct_field(ga["id"], "invoice_number", "INV-GROUP-1", "renumbered back", "r")
    check("changing the key back restores the relationship in upload order",
          dups([ga["id"], gb["id"]]) == {ga["id"]: None, gb["id"]: ga["id"]} and has_dup_issue(gb["id"]))
    gc, _ = s.ingest("u", "gc.pdf", pdf(), "application/pdf"); s.process(gc["id"])
    check("a third copy also points at the single original", dups([ga["id"], gb["id"], gc["id"]]) == {ga["id"]: None, gb["id"]: ga["id"], gc["id"]: ga["id"]})
    s.correct_field(ga["id"], "invoice_number", "INV-GROUP-7", "leaves the group", "r")
    d3 = dups([ga["id"], gb["id"], gc["id"]])
    check("when the original leaves, the next-oldest becomes the single original",
          d3 == {ga["id"]: None, gb["id"]: None, gc["id"]: gb["id"]}, str(d3))

    # an older upload processed AFTER a newer one still becomes the original and demotes the newer one
    LV = dict(BASE, invoice_number="INV-LATE-1")
    ex = Fake(script=outcome(LV))
    s = svc(ex)
    old_up, _ = s.ingest("u", "old.pdf", pdf(), "application/pdf")       # uploaded first, processed last
    new_up, _ = s.ingest("u", "new.pdf", pdf(), "application/pdf")
    s.process(new_up["id"]); s.approve(new_up["id"], "r")
    check("before the older upload is processed the newer one is the only member", s.get_document(new_up["id"])["status"] == "approved")
    s.process(old_up["id"])
    d = s.get_document(new_up["id"])
    check("the older upload becomes the original and the already-approved newer copy is demoted for review",
          dups([old_up["id"], new_up["id"]]) == {old_up["id"]: None, new_up["id"]: old_up["id"]}
          and d["status"] == "needs_review" and d["approved_by"] is None and has_dup_issue(new_up["id"]), str(d["status"]))

    # ---------------- integrity ----------------
    s = svc()
    d, _ = s.ingest("u", "t.pdf", pdf(), "application/pdf")
    key = db.sql('SELECT storage_key FROM "MSME Document" WHERE id = ?', [d["id"]])[0]["storage_key"]
    with open(os.path.join(storage_dir, key), "ab") as fh:
        fh.write(b"tamper")
    check("tampered file is refused on read", raises("storage_corrupt", s.read_original, d["id"]))
    check("tampered file fails processing with storage_corrupt", s.process(d["id"])["error_code"] == "storage_corrupt")

    # ---------------- sweep + listing ----------------
    s = svc(Fake())
    q1, _ = s.ingest("u", "q1.pdf", pdf(), "application/pdf"); q2, _ = s.ingest("u", "q2.pdf", pdf(), "application/pdf")
    out = s.sweep(50)
    check("sweep processes queued documents", out["processed"] >= 2 and s.get_document(q1["id"])["status"] != "uploaded"
          and s.get_document(q2["id"])["status"] != "uploaded", str(out))
    page = s.list_documents(limit=2, offset=0)
    check("listing paginates and reports total", len(page["rows"]) == 2 and page["total"] > 2 and "storage_key" not in page["rows"][0])
    check("listing filters by status", all(r["status"] == "failed" for r in s.list_documents(status="failed")["rows"]))
    check("unknown document -> not_found", raises("not_found", s.get_document, "MDOC-000000000000"))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
