#!/usr/bin/env python3
"""C-1/C-2: compliance workspace service behaviour on a real database.

Covers profile validation, idempotent task generation, deadline overrides, derived overdue state, the full
workpaper lifecycle (draft -> submit -> approve/reject -> record filing), maker-checker, stale-figure guards,
figures derived from real approved documents, concurrent approvals, concurrent audit appends, hash-chain
tamper detection and the PDF/CSV reports.

Run:  python -m tests.test_msme_compliance      (LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)
"""

import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

sys.dont_write_bytecode = True

CO_GSTIN, OTHER_A, OTHER_B = "27AAPFU0939F1ZV", "09AAACT2727Q1ZU", "27AABCU9603R1ZN"


def main() -> int:
    from tests._msme_db import assert_backend, reset_test_db

    db_path = reset_test_db("lambda_msme_compliance_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_comp_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-compliance"})
    os.environ.pop("MSME_REQUIRE_DISTINCT_APPROVER", None)

    from lambda_erp.database import get_db, setup
    setup(db_path)
    assert_backend(bool(os.environ.get("LAMBDA_ERP_TEST_DB")))

    from msme.compliance import audit, report
    from msme.compliance.service import ComplianceError, ComplianceService
    from msme.documents.extraction import ExtractedField, ExtractionOutcome
    from msme.documents.service import DocumentService
    from msme.documents.storage import LocalDocumentStorage

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    def raises(code, fn, *a, **k):
        try:
            fn(*a, **k)
        except ComplianceError as exc:
            return exc.code == code
        return False

    db = get_db()
    NOW = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    svc = ComplianceService(clock=lambda: NOW)
    for name in ("Acme Traders", "Beta QRMP Co", "Gamma Co", "Delta Co") + tuple(f"Race Co {i}" for i in range(6)):
        db.insert("Company", {"name": name, "company_name": name, "default_currency": "INR"})
    db.commit()

    # ---------------- documents (real Pass-4 service) ----------------
    n = [0]
    current = {"values": None, "type": "invoice"}

    class Fake:
        name, version = "fake", "1"

        def extract(self, path):
            fields = [ExtractedField(p, v, 0.97, 1, v, {"x0": .1, "y0": .1, "x1": .2, "y1": .2}, "verified")
                      for p, v in current["values"].items()]
            return ExtractionOutcome("extracted", current["type"], fields, [], extractor="fake", extractor_version="1")

    docs = DocumentService(Fake(), LocalDocumentStorage(storage_dir))

    def make_doc(number, issue_date, subtotal, tax, seller=CO_GSTIN, buyer=OTHER_A, doc_type="invoice",
                 currency="INR", approve=True):
        total = f"{float(subtotal) + float(tax):.2f}"
        current["values"] = {"invoice_number": number, "issue_date": issue_date, "seller.name": "S",
                             "seller.tax_ids[0].value": seller, "seller.tax_ids[0].scheme": "gst",
                             "buyer.tax_ids[0].value": buyer, "buyer.tax_ids[0].scheme": "gst",
                             "subtotal": subtotal, "tax_amount": tax, "total_amount": total, "currency": currency}
        current["type"] = doc_type
        n[0] += 1
        d, _ = docs.ingest("u", f"d{n[0]}.pdf", f"%PDF-1.4\n% comp {n[0]}\n%%EOF\n".encode(), "application/pdf")
        docs.process(d["id"])
        if approve:
            docs.approve(d["id"], "reviewer")
        return d["id"]

    out1 = make_doc("OUT-1", "2026-06-10", "1000.00", "180.00")
    out2 = make_doc("OUT-2", "2026-06-20", "2000.00", "360.00")
    in1 = make_doc("IN-1", "2026-06-15", "500.00", "90.00", seller=OTHER_A, buyer=CO_GSTIN)
    unclassified = make_doc("X-1", "2026-06-11", "100.00", "18.00", seller=OTHER_A, buyer=OTHER_B)
    credit = make_doc("CN-1", "2026-06-12", "50.00", "9.00", doc_type="credit_note")
    usd = make_doc("USD-1", "2026-06-13", "100.00", "0.00", currency="USD")
    make_doc("OUT-1", "2026-06-10", "1000.00", "180.00")          # same business key, different bytes -> duplicate
    dup_id = db.sql('SELECT id FROM "MSME Document" WHERE duplicate_of IS NOT NULL')[0]["id"]
    out_of_period = make_doc("OUT-JULY", "2026-07-01", "999.00", "0.00")

    # ---------------- profile ----------------
    check("unknown company rejected", raises("company_not_found", svc.set_profile, "Nope", "regular_monthly", None, None, "alice"))
    check("invalid filer type rejected", raises("invalid_filer_type", svc.set_profile, "Acme Traders", "weekly", None, None, "alice"))
    check("QRMP requires an explicit state category (never inferred)", raises("state_category_required", svc.set_profile, "Beta QRMP Co", "qrmp", None, None, "alice"))
    check("state category must be X or Y", raises("invalid_state_category", svc.set_profile, "Beta QRMP Co", "qrmp", "Z", None, "alice"))
    check("a GSTIN that fails the check character is rejected", raises("invalid_gstin", svc.set_profile, "Acme Traders", "regular_monthly", None, "27AAPFU0939F1ZW", "alice"))
    p = svc.set_profile("Acme Traders", "regular_monthly", None, "27aapfu0939f1zv", "alice")
    check("valid profile stored with normalised GSTIN", p["gstin"] == CO_GSTIN and p["filer_type"] == "regular_monthly")
    check("tasks cannot be generated without a profile", raises("profile_required", svc.generate_tasks, "Gamma Co", 2026, "alice"))

    # ---------------- generation: idempotent, rule-driven ----------------
    g = svc.generate_tasks("Acme Traders", 2026, "alice")
    check("regular monthly FY2026-27 generates 25 tasks", g["created"] == 25 and g["unchanged"] == 0, str(g))
    events_after_first = len(svc.audit_events(limit=500))
    g2 = svc.generate_tasks("Acme Traders", 2026, "alice")
    check("regeneration is idempotent (nothing created/updated, no audit noise)",
          g2["created"] == 0 and g2["updated"] == 0 and g2["unchanged"] == 25 and len(svc.audit_events(limit=500)) == events_after_first, str(g2))
    check("generation reports that extensions are not applied", "extensions are not applied" in g["note"])
    tasks = {(t["form"], t["period"]): t for t in svc.list_tasks("Acme Traders", 2026, limit=500)["rows"]}
    t3b = tasks[("GSTR-3B", "2026-06")]
    check("GSTR-3B June 2026 due 20 July from versioned rules", t3b["due_date"] == "2026-07-20" and t3b["basis"] == "STATUTORY_DEFAULT" and t3b["rule_version"] == "default-2021")
    check("overdue is derived from the clock (25 Jul 2026)", t3b["overdue"] and t3b["days_to_due"] == -5 and not tasks[("GSTR-3B", "2026-07")]["overdue"])
    page = svc.list_tasks("Acme Traders", 2026, limit=10, offset=20)
    check("listing paginates", page["total"] == 25 and len(page["rows"]) == 5)
    check("status filter works", svc.list_tasks("Acme Traders", status="open", limit=500)["total"] == 25)

    qp = svc.set_profile("Beta QRMP Co", "qrmp", "X", None, "alice")
    svc.generate_tasks("Beta QRMP Co", 2026, "alice")
    q = {(t["form"], t["period"]): t for t in svc.list_tasks("Beta QRMP Co", 2026, limit=500)["rows"]}
    check("QRMP category X: GSTR-3B for Apr-Jun is due 22 July", q[("GSTR-3B", "2026-04..2026-06")]["due_date"] == "2026-07-22")
    svc.set_profile("Beta QRMP Co", "qrmp", "Y", None, "alice")
    g = svc.generate_tasks("Beta QRMP Co", 2026, "alice")
    q = {(t["form"], t["period"]): t for t in svc.list_tasks("Beta QRMP Co", 2026, limit=500)["rows"]}
    check("changing the category updates only the four GSTR-3B quarters", g["updated"] == 4 and q[("GSTR-3B", "2026-04..2026-06")]["due_date"] == "2026-07-24", str(g))
    up = [(t["form"], t["period"]) for t in svc.upcoming("Beta QRMP Co", 30)]
    check("upcoming hides optional tasks nobody started (IFF) but lists overdue work",
          not any(f == "IFF" for f, _ in up) and ("GSTR-1", "2026-04..2026-06") in up)

    # ---------------- overrides ----------------
    check("override requires a cited source", raises("source_required", svc.add_override, "Acme Traders", "GSTR-3B", "2026-06", "2026-07-31", "n/a", "alice"))
    check("override validates the date", raises("invalid_value", svc.add_override, "Acme Traders", "GSTR-3B", "2026-06", "31-07-2026", "Notification 10/2026 (test)", "alice"))
    svc.add_override("Acme Traders", "GSTR-3B", "2026-06", "2026-07-31", "Notification 10/2026-CT (test)", "alice")
    t3b = svc.get_task(t3b["id"])
    check("override moves the existing task and marks its basis", t3b["due_date"] == "2026-07-31" and t3b["basis"] == "EXTENSION_OVERRIDE" and not t3b["overdue"])
    g = svc.generate_tasks("Acme Traders", 2026, "alice")
    check("regeneration keeps the override", g["updated"] == 0 and svc.get_task(t3b["id"])["due_date"] == "2026-07-31", str(g))

    # ---------------- workpaper: figures from approved documents ----------------
    check("approved documents exist before preparation", all(docs.get_document(i)["status"] == "approved" for i in (out1, out2, in1)))
    wp = svc.open_workpaper(t3b["id"], "alice")
    f = wp["content"]["figures"]
    check("outward figures: 2 documents, taxable 3000.00, tax 540.00", f["outward"] == {"count": 2, "taxable_value": "3000.00", "tax": "540.00", "total": "3540.00"}, str(f["outward"]))
    check("inward figures: 1 document, taxable 500.00, tax 90.00", f["inward"]["count"] == 1 and f["inward"]["tax"] == "90.00", str(f["inward"]))
    att = f["attention"]
    check("unsafe-to-sum documents are listed, not silently dropped",
          att.get("unclassified") == [unclassified] and att.get("credit_notes") == [credit] and att.get("non_inr") == [usd]
          and att.get("excluded_duplicates") == [dup_id], str(att))
    check("a document outside the period is not included", out_of_period not in {d["id"] for d in f["documents"]} and out_of_period not in sum(att.values(), []))
    check("figures carry the not-a-return disclaimer", "not a return" in f["disclaimer"])
    check("opening a workpaper moves the task to in_preparation", svc.get_task(t3b["id"])["status"] == "in_preparation")
    check("opening again returns the same draft", svc.open_workpaper(t3b["id"], "alice")["id"] == wp["id"])
    check("checklist is labelled internal guidance", wp["content"]["checklist_source"] == "INTERNAL_GUIDANCE")

    check("submit blocked while required items are pending", raises("checklist_incomplete", svc.submit_workpaper, wp["id"], "alice"))
    check("unknown checklist item rejected", raises("unknown_item", svc.update_draft, wp["id"], "alice", [{"key": "nope", "status": "done"}]))
    check("invalid item status rejected", raises("invalid_status", svc.update_draft, wp["id"], "alice", [{"key": "outward_tax", "status": "maybe"}]))
    check("unknown document reference rejected", raises("unknown_document", svc.update_draft, wp["id"], "alice", [{"key": "outward_tax", "document_ids": ["MDOC-ffffffffffff"]}]))
    check("over-long note rejected", raises("note_too_long", svc.update_draft, wp["id"], "alice", [{"key": "outward_tax", "note": "x" * 501}]))
    req = [i["key"] for i in wp["content"]["checklist"] if i["required"]]
    svc.update_draft(wp["id"], "alice", [{"key": k, "status": "done", "note": "checked"} for k in req]
                     + [{"key": "outward_tax", "document_ids": [out1, out2]}], notes="<script>alert(1)</script> note")
    sub = svc.submit_workpaper(wp["id"], "alice")
    check("submitted workpaper is frozen and the task is in_review", sub["status"] == "submitted" and svc.get_task(t3b["id"])["status"] == "in_review")
    check("submitted workpaper cannot be edited", raises("not_editable", svc.update_draft, wp["id"], "alice", None, "x"))
    check("a second workpaper cannot be opened while one is in review", raises("workpaper_in_review", svc.open_workpaper, t3b["id"], "bob"))
    check("maker cannot approve their own workpaper", raises("maker_checker", svc.approve_workpaper, wp["id"], "alice"))

    # concurrent approvals: exactly one wins
    outcomes = []

    def approve(user):
        try:
            svc.approve_workpaper(wp["id"], user)
            outcomes.append("ok")
        except ComplianceError as exc:
            outcomes.append(exc.code)

    ts = [threading.Thread(target=approve, args=(u,)) for u in ("bob", "carol")]
    [t.start() for t in ts]; [t.join() for t in ts]
    check("two concurrent approvals: exactly one succeeds", outcomes.count("ok") == 1 and len(outcomes) == 2, str(outcomes))
    ap = [e for e in svc.audit_events(wp["id"]) if e["action"] == "workpaper.approve"]
    check("exactly one approval event in the audit trail", len(ap) == 1 and '"maker_checker":"enforced"' in ap[0]["detail"])
    check("approved workpaper moves the task to approved_for_filing", svc.get_task(t3b["id"])["status"] == "approved_for_filing")

    # ---------------- stale figures after approval ----------------
    docs.correct_field(out2, "invoice_number", "OUT-2A", "typo in invoice number", "reviewer")   # revokes its approval
    check("changing a source document after approval blocks recording the filing",
          raises("figures_stale", svc.record_filing, t3b["id"], "bob", "AA2706260001", "2026-07-24"))
    wp2 = svc.open_workpaper(t3b["id"], "alice")
    check("a new revision supersedes the approval and reopens preparation",
          wp2["version"] == 2 and svc.get_task(t3b["id"])["status"] == "in_preparation"
          and all(i["status"] == "done" for i in wp2["content"]["checklist"] if i["required"]))
    check("the revision's figures reflect the document no longer being approved", wp2["content"]["figures"]["outward"]["count"] == 1)
    docs.approve(out2, "reviewer") if docs.get_document(out2)["status"] in ("extracted", "needs_review") else None
    wp2 = svc.update_draft(wp2["id"], "alice", refresh_figures=True)
    check("refreshing figures picks up the re-approved document", wp2["content"]["figures"]["outward"]["count"] == 2)
    svc.submit_workpaper(wp2["id"], "alice")
    # staleness between submission and approval
    docs.correct_field(out1, "seller.name", "Acme Supplies Ltd", "legal name fix", "reviewer")
    check("approval blocked if sources changed after submission", raises("figures_stale", svc.approve_workpaper, wp2["id"], "bob"))
    check("rejection needs a reason", raises("reason_required", svc.reject_workpaper, wp2["id"], "bob", " "))
    rej = svc.reject_workpaper(wp2["id"], "bob", "Source invoice changed; re-prepare")
    check("rejection recorded and the task returns to preparation", rej["status"] == "rejected" and rej["decision_note"].startswith("Source") and svc.get_task(t3b["id"])["status"] == "in_preparation")
    docs.approve(out1, "reviewer")
    wp3 = svc.open_workpaper(t3b["id"], "alice")
    wp3 = svc.update_draft(wp3["id"], "alice", refresh_figures=True)
    check("an edited ORIGINAL invoice stays in the figures (regression: it was once excluded as its own duplicate)",
          wp3["content"]["figures"]["outward"]["count"] == 2 and out1 in {d["id"] for d in wp3["content"]["figures"]["documents"]}
          and wp3["content"]["figures"]["attention"].get("excluded_duplicates") == [dup_id], str(wp3["content"]["figures"]["attention"]))
    svc.submit_workpaper(wp3["id"], "alice")
    svc.approve_workpaper(wp3["id"], "bob")

    # ---------------- recording a filing (user-declared) ----------------
    check("filing cannot be recorded before approval", raises("not_approved", svc.record_filing, tasks[("GSTR-1", "2026-06")]["id"], "bob", "AA2706260002", "2026-07-10"))
    check("reference format enforced", raises("invalid_reference", svc.record_filing, t3b["id"], "bob", "<script>", "2026-07-24"))
    check("filing date cannot be in the future", raises("invalid_date", svc.record_filing, t3b["id"], "bob", "AA2706260001", "2026-08-01"))
    done = svc.record_filing(t3b["id"], "bob", "AA2706260001", "2026-07-24", "Filed from the portal")
    check("filing_recorded carries the unverified label and the user's reference",
          done["status"] == "filing_recorded" and done["filing_reference"] == "AA2706260001"
          and "not verified" in done["filing_note_label"].lower() and not done["overdue"])
    fe = [e for e in svc.audit_events(t3b["id"]) if e["action"] == "filing.recorded_by_user"][0]
    check("audit event states it was not verified against the portal", '"verified_against_portal":false' in fe["detail"])
    check("a closed task rejects new workpapers and second filings",
          raises("task_closed", svc.open_workpaper, t3b["id"], "alice") and raises("not_approved", svc.record_filing, t3b["id"], "bob", "AA2706260009", "2026-07-24"))

    # ---------------- maker-checker waiver is explicit and audited ----------------
    t1 = tasks[("GSTR-1", "2026-06")]
    w = svc.open_workpaper(t1["id"], "solo")
    svc.update_draft(w["id"], "solo", [{"key": k["key"], "status": "done"} for k in w["content"]["checklist"] if k["required"]])
    svc.submit_workpaper(w["id"], "solo")
    os.environ["MSME_REQUIRE_DISTINCT_APPROVER"] = "0"
    try:
        svc.approve_workpaper(w["id"], "solo")
    finally:
        os.environ.pop("MSME_REQUIRE_DISTINCT_APPROVER")
    ev = [e for e in svc.audit_events(w["id"]) if e["action"] == "workpaper.approve"][0]
    check("self-approval only with an explicit policy waiver, recorded in the audit event", '"maker_checker":"waived_by_policy"' in ev["detail"])

    # ---------------- no GSTIN on the profile: direction cannot be determined ----------------
    svc.set_profile("Gamma Co", "regular_monthly", None, None, "alice")
    svc.generate_tasks("Gamma Co", 2026, "alice")
    gt = [t for t in svc.list_tasks("Gamma Co", 2026, limit=500)["rows"] if t["form"] == "GSTR-3B" and t["period"] == "2026-06"][0]
    gf = svc.open_workpaper(gt["id"], "alice")["content"]["figures"]
    check("without a company GSTIN nothing is counted as sales or purchases",
          gf["outward"]["count"] == 0 and gf["inward"]["count"] == 0 and gf["gstin"] is None
          and {out1, out2, in1} <= set(gf["attention"].get("unclassified", [])), str(gf["attention"]))

    # ---------------- reports ----------------
    import io
    import pdfplumber
    pdf = report.workpaper_pdf(svc, wp3["id"])
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = " ".join(" ".join((p.extract_text() or "").split()) for p in doc.pages)  # unwrap line breaks
    check("PDF is a real PDF with the not-a-return banner", pdf[:5] == b"%PDF-" and "NOT A FILED RETURN" in text, text[:120])
    check("PDF shows the user-recorded filing as unverified",
          "Filing record (entered by the user)" in text and "AA2706260001" in text
          and "Not verified against the GST portal" in text, text[-500:])
    check("PDF shows rule version, due date and audit head", "default-2021" in text and "2026-07-31" in text and "Audit chain head" in text)
    html_text = report.workpaper_html(svc, wp["id"])   # the first workpaper holds the hostile note
    check("user text is HTML-escaped in the report", "<script>alert(1)</script>" not in html_text and "&lt;script&gt;" in html_text)
    check("csv_safe neutralises formula prefixes", all(report.csv_safe(x).startswith("'") for x in ("=SUM(1)", "+1", "-2+3", "@cmd", "\tx")) and report.csv_safe("ok") == "ok")
    csv_text = report.calendar_csv([dict(svc.list_tasks("Acme Traders", 2026, limit=1)["rows"][0], company='=HYPERLINK("http://x")')])
    check("calendar csv is formula-safe and has a header", csv_text.splitlines()[0].startswith("company,fy_start_year") and "'=HYPERLINK" in csv_text, csv_text[:200])

    # ---------------- audit chain ----------------
    for name in (f"Race Co {i}" for i in range(6)):
        pass
    errs = []

    def race(i):
        try:
            svc.set_profile(f"Race Co {i}", "regular_monthly", None, None, f"racer{i}")
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    ts = [threading.Thread(target=race, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    v = svc.verify_audit()
    check("6 concurrent audited writes all succeed and the chain stays valid and gap-free", not errs and v["ok"] and v["head"]["seq"] == v["events"], f"{errs} {v}")
    total = v["events"]
    ev = db.sql('SELECT id, detail FROM "MSME Audit Event" WHERE seq = 3')[0]
    db.sql('UPDATE "MSME Audit Event" SET detail = ? WHERE id = ?', ['{"forged":true}', ev["id"]]); db.commit()
    v = svc.verify_audit()
    check("editing a past event is detected at that event", not v["ok"] and v["first_bad_seq"] == 3 and "altered" in v["reason"], str(v))
    db.sql('UPDATE "MSME Audit Event" SET detail = ? WHERE id = ?', [ev["detail"], ev["id"]]); db.commit()
    check("restoring the event restores validity", svc.verify_audit()["ok"])
    row5 = db.sql('SELECT * FROM "MSME Audit Event" WHERE seq = 5')[0]
    db.sql('DELETE FROM "MSME Audit Event" WHERE seq = 5'); db.commit()
    v = svc.verify_audit()
    check("deleting an event is detected as a gap", not v["ok"] and v["first_bad_seq"] == 5 and "gap" in v["reason"], str(v))
    db.insert("MSME Audit Event", {k: row5[k] for k in row5.keys()}); db.commit()
    check("re-inserting the original event restores validity", svc.verify_audit()["ok"] and svc.verify_audit()["events"] == total)
    check("every state change in this run is in the trail", {e["action"] for e in svc.audit_events(limit=500)} >=
          {"profile.set", "tasks.generate", "override.set", "workpaper.open", "workpaper.update", "workpaper.submit",
           "workpaper.approve", "workpaper.reject", "filing.recorded_by_user"})

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
