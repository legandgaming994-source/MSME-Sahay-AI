#!/usr/bin/env python3
"""R-1/R-2: credit-readiness and scheme navigation on a real database.

Covers the facts registry and its append-only history, DERIVED MSME classification (effective-dated), scheme
assessments across NOT_MET / APPEARS_MET / INSUFFICIENT_EVIDENCE with evidence and "what to provide next",
immutable audited snapshots, readiness evidence from REAL bank-statement imports, REAL compliance tasks and REAL
approved documents, attestations, the PDF summary, and the audit chain.

Run:  python -m tests.test_msme_credit      (LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)
"""

import io
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True

CO_GSTIN, OTHER = "27AAPFU0939F1ZV", "09AAACT2727Q1ZU"


def main() -> int:
    from tests._msme_db import assert_backend, reset_test_db

    db_path = reset_test_db("lambda_msme_credit_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_credit_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-credit"})

    from lambda_erp.database import get_db, setup
    setup(db_path)
    assert_backend(bool(os.environ.get("LAMBDA_ERP_TEST_DB")))

    from msme.compliance.service import ComplianceService
    from msme.credit import report
    from msme.credit.service import CreditError, CreditService
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
        except CreditError as exc:
            return exc.code == code
        return False

    now = {"t": datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)}
    clock = lambda: now["t"]  # noqa: E731
    svc, comp = CreditService(clock=clock), ComplianceService(clock=clock)
    db = get_db()
    for name in ("Acme Traders", "Other Co", "Race Co"):
        db.insert("Company", {"name": name, "company_name": name, "default_currency": "INR"})
    db.insert("Account", {"name": "Bank - A", "account_name": "Bank", "company": "Acme Traders", "root_type": "Asset",
                          "report_type": "Balance Sheet", "is_group": 0})
    db.insert("Account", {"name": "Bank - O", "account_name": "Bank O", "company": "Other Co", "root_type": "Asset",
                          "report_type": "Balance Sheet", "is_group": 0})
    for i, (co, acct) in enumerate((("Acme Traders", "Bank - A"), ("Other Co", "Bank - O"))):
        db.insert("Bank Account", {"name": f"BA-{i}", "account_name": f"BA {i}", "company": co, "account": acct,
                                   "iban": f"IN00SYNTH{i:010d}", "currency": "INR"})
    db.commit()
    bsi = [0]

    def statement(company, start, end, docstatus=1, bank="BA-0"):
        bsi[0] += 1
        db.insert("Bank Statement Import", {"name": f"BSI-{bsi[0]}", "bank_account": bank, "company": company,
                                            "source_sha256": f"{bsi[0]:064d}", "from_date": start, "to_date": end,
                                            "docstatus": docstatus, "status": "Imported"})
        db.commit()

    # ---------------- facts registry ----------------
    check("unknown company rejected", raises("company_not_found", svc.set_fact, "Nope", "sector", "service", "alice"))
    check("unknown fact rejected", raises("unknown_fact", svc.set_fact, "Acme Traders", "favourite_colour", "x", "alice"))
    check("msme_category can never be typed in", raises("derived_fact", svc.set_fact, "Acme Traders", "msme_category", "Micro", "alice"))
    check("booleans must be real booleans", raises("invalid_value", svc.set_fact, "Acme Traders", "udyam_registered", "yes", "alice"))
    check("negative amounts rejected", raises("invalid_value", svc.set_fact, "Acme Traders", "investment_inr", -5, "alice"))
    check("zero credit amount rejected", raises("invalid_value", svc.set_fact, "Acme Traders", "credit_facility_amount", 0, "alice"))
    check("sector must be one of the listed values", raises("invalid_value", svc.set_fact, "Acme Traders", "sector", "farming", "alice"))
    check("malformed Udyam number rejected", raises("invalid_udyam", svc.set_fact, "Acme Traders", "udyam_number", "UDYAM-MH-0-12345", "alice"))
    check("unknown evidence document rejected", raises("unknown_document", svc.set_fact, "Acme Traders", "turnover_inr", 1, "alice", "MDOC-ffffffffffff"))
    u = svc.set_fact("Acme Traders", "udyam_number", " udyam-mh-27-0012345 ", "alice")
    check("Udyam number normalised and stored with its source", u["value"] == "UDYAM-MH-27-0012345" and u["source"] == "user_declared" and not u["unchanged"])
    n0 = len(db.sql('SELECT id FROM "MSME Credit Fact"'))
    again = svc.set_fact("Acme Traders", "udyam_number", "UDYAM-MH-27-0012345", "alice")
    check("re-entering the same value is a no-op (no row, no audit noise)", again["unchanged"] and len(db.sql('SELECT id FROM "MSME Credit Fact"')) == n0)
    svc.set_fact("Acme Traders", "credit_facility_amount", 400000, "alice")
    now["t"] += timedelta(seconds=1)
    svc.set_fact("Acme Traders", "credit_facility_amount", 1500000, "bob")
    hist = svc.fact_history("Acme Traders", "credit_facility_amount")
    cur = {f["key"]: f for f in svc.get_facts("Acme Traders")["facts"]}["credit_facility_amount"]
    check("history is append-only and the newest value is current", [h["value"] for h in hist] == [400000, 1500000] and cur["value"] == 1500000 and cur["entered_by"] == "bob")
    check("facts are scoped to their company", svc.get_facts("Other Co")["facts"] == [])

    # ---------------- derived classification (effective-dated) ----------------
    g = svc.get_facts("Acme Traders")
    check("no investment/turnover: classification is insufficient and no category is derived",
          g["classification"]["status"] == "INSUFFICIENT_EVIDENCE" and g["derived"] == [])
    svc.set_fact("Acme Traders", "investment_inr", 20_000_000, "alice")
    g = svc.get_facts("Acme Traders")
    check("partial evidence gives a lower bound, not a category", g["classification"]["status"] == "INSUFFICIENT_EVIDENCE" and g["classification"]["minimum_category"] == "Micro" and g["derived"] == [])
    svc.set_fact("Acme Traders", "turnover_inr", 80_000_000, "alice")
    g = svc.get_facts("Acme Traders")
    check("2 crore investment + 8 crore turnover -> Micro under S.O. 1364(E) (derived, with its source)",
          g["classification"]["category"] == "Micro" and g["derived"][0]["source"] == "derived:msme-classification@S.O.1364(E)-2025-03-21", str(g["derived"]))
    svc.set_fact("Acme Traders", "turnover_inr", 150_000_000, "alice")
    check("15 crore turnover pushes the derived category to Small", svc.get_facts("Acme Traders")["classification"]["category"] == "Small")

    # ---------------- scheme assessment: no facts, partial, met, not met ----------------
    empty = svc.assess("Other Co", "alice")
    by = {a["scheme_id"]: a for a in empty["assessments"]}
    check("with no facts every scheme is INSUFFICIENT_EVIDENCE", {a["status"] for a in by.values()} == {"INSUFFICIENT_EVIDENCE"}, str({k: v["status"] for k, v in by.items()}))
    check("missing facts are named with plain-language descriptions",
          {"non_corporate_non_farm", "credit_facility_amount"} <= {m["key"] for m in by["pmmy-mudra"]["missing_facts"]}
          and all(m["description"] for m in by["pmmy-mudra"]["missing_facts"]))
    check("a missing category asks for investment and turnover, not for the category itself",
          {"investment_inr", "turnover_inr"} <= {m["key"] for m in by["cgtmse"]["missing_facts"]}
          and "msme_category" not in {m["key"] for m in by["cgtmse"]["missing_facts"]})
    check("results always require a lender decision and never say 'eligible'",
          all(a["lender_decision_required"] for a in by.values()) and not any("ELIGIBLE" in a["status"].replace("NOT_", "") for a in by.values()))

    for k, v in (("udyam_registered", True), ("sector", "service"), ("collateral_free_requested", True)):
        svc.set_fact("Acme Traders", k, v, "alice")
    res = {a["scheme_id"]: a for a in svc.assess("Acme Traders", "alice")["assessments"]}
    cg = res["cgtmse"]
    check("CGTMSE: Small + Udyam + service + collateral-free -> criteria APPEAR met (not an approval)", cg["status"] == "CRITERIA_APPEARS_MET", str(cg["criteria"]))
    srcs = {c["id"]: c["evidence_source"] for c in cg["criteria"]}
    check("each criterion names its evidence source (user vs derived)",
          srcs["udyam_registered"] == "user_declared" and srcs["micro_or_small"].startswith("derived:msme-classification@"))
    check("unverified CGTMSE details are listed, not asserted", any("percentages" in u for u in cg["unverified_details"]) and cg["source"]["verification"] == "SECONDARY_ONLY")
    pm = res["pmmy-mudra"]
    check("PMMY with a 15 lakh loan but unknown business type/prior loan -> insufficient, asks for both",
          pm["status"] == "INSUFFICIENT_EVIDENCE" and {"non_corporate_non_farm", "prior_tarun_loan_repaid"} == {m["key"] for m in pm["missing_facts"]}, str(pm["missing_facts"]))
    svc.set_fact("Acme Traders", "non_corporate_non_farm", True, "alice")
    svc.set_fact("Acme Traders", "prior_tarun_loan_repaid", False, "alice")
    check("PMMY Tarun Plus without a repaid Tarun loan -> NOT_MET", {a["scheme_id"]: a for a in svc.assess("Acme Traders", "alice")["assessments"]}["pmmy-mudra"]["status"] == "CRITERIA_NOT_MET")
    svc.set_fact("Acme Traders", "prior_tarun_loan_repaid", True, "alice")
    check("with a repaid Tarun loan the criteria appear met", {a["scheme_id"]: a for a in svc.assess("Acme Traders", "alice")["assessments"]}["pmmy-mudra"]["status"] == "CRITERIA_APPEARS_MET")
    svc.set_fact("Acme Traders", "credit_facility_amount", 2_500_000, "alice")
    check("above the 20 lakh PMMY limit -> NOT_MET", {a["scheme_id"]: a for a in svc.assess("Acme Traders", "alice")["assessments"]}["pmmy-mudra"]["status"] == "CRITERIA_NOT_MET")
    svc.set_fact("Acme Traders", "investment_inr", 2_600_000_000, "alice")
    big = {a["scheme_id"]: a for a in svc.assess("Acme Traders", "alice")["assessments"]}
    check("an enterprise above the Medium limits derives NOT_MSME and fails the CGTMSE category criterion",
          big["cgtmse"]["status"] == "CRITERIA_NOT_MET" and any(c["id"] == "micro_or_small" and c["status"] == "NOT_MET" for c in big["cgtmse"]["criteria"]))

    # ---------------- snapshots are persisted, immutable and audited ----------------
    hist = svc.assessments("Acme Traders", "cgtmse")
    check("every assessment run left an immutable snapshot (newest first) with rule version and digest",
          len(hist) >= 4 and hist[0]["status"] == "CRITERIA_NOT_MET" and hist[-1]["rule_version"] == "cgtmse-revamp-2023-04-01" and len(hist[0]["facts_digest"]) == 64)
    digests = {h["facts_digest"] for h in svc.assessments("Acme Traders", "cgtmse")}
    check("changed facts produce a different digest", len(digests) >= 3)
    check("snapshot listing can be filtered by scheme and is company-scoped",
          all(h["scheme_id"] == "cgtmse" for h in hist) and svc.assessments("Other Co", "cgtmse")[0]["status"] == "INSUFFICIENT_EVIDENCE")
    ev = [e for e in comp.audit_events(limit=500) if e["action"] == "scheme.assess"]
    check("each persisted assessment is an audit event naming the facts digest", ev and '"facts_digest"' in ev[0]["detail"])

    # ---------------- effective dating ----------------
    now["t"] = datetime(2023, 3, 31, 10, 0, tzinfo=timezone.utc)
    old = svc.assess("Acme Traders", "alice")
    by = {a["scheme_id"]: a for a in old["assessments"]}
    check("before 1 Apr 2023 CGTMSE rules are not available (no guess)", by["cgtmse"]["status"] == "NOT_AVAILABLE_ON_DATE" and "cgtmse" not in old["snapshot_ids"])
    check("classification uses the 2020 limits on that date", old["classification"]["rule_version"] == "S.O.2119(E)-2020-06-26")
    now["t"] = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    check("catalogue shows current versions with source and verification status",
          {s["id"]: s["source"]["verification"] for s in svc.catalogue(now["t"].date())} == {"pmmy-mudra": "PRIMARY_PIB", "cgtmse": "SECONDARY_ONLY"})

    # ---------------- readiness ----------------
    r0 = svc.readiness("Other Co")
    st = {i["key"]: i["status"] for i in r0["items"]}
    check("a company with nothing starts honest: unknown/missing, never 'ready'",
          st["udyam_registration"] == "UNKNOWN" and st["bank_statements"] == "MISSING" and st["gst_return_history"] == "UNKNOWN"
          and st["sales_invoice_evidence"] == "UNKNOWN" and st["financial_statements"] == "MISSING")
    check("requirements are labelled unverified-typical and there is no composite score",
          r0["requirement_basis"] == "UNVERIFIED_TYPICAL" and all(i["requirement_basis"] == "UNVERIFIED_TYPICAL" for i in r0["items"]) and "score" not in r0)

    today = now["t"].date()
    statement("Acme Traders", (today - timedelta(days=364)).isoformat(), (today - timedelta(days=200)).isoformat())
    statement("Acme Traders", (today - timedelta(days=150)).isoformat(), today.isoformat())
    statement("Acme Traders", (today - timedelta(days=199)).isoformat(), (today - timedelta(days=160)).isoformat(), docstatus=2)  # cancelled
    statement("Other Co", (today - timedelta(days=364)).isoformat(), today.isoformat(), bank="BA-1")                              # another company
    bank = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["bank_statements"]
    gap = bank["evidence"][0]["detail"]["gaps"]
    check("bank statements: partial coverage with the exact gap listed (cancelled import ignored)",
          bank["status"] == "PARTIAL" and len(gap) == 1 and gap[0]["from"] == (today - timedelta(days=199)).isoformat()
          and gap[0]["to"] == (today - timedelta(days=151)).isoformat() and gap[0]["days"] == 49, str(bank["evidence"][0]["detail"]))
    check("another company's statements do not count", {i["key"]: i for i in svc.readiness("Other Co")["items"]}["bank_statements"]["status"] == "EVIDENCED")
    statement("Acme Traders", (today - timedelta(days=199)).isoformat(), (today - timedelta(days=151)).isoformat())
    bank = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["bank_statements"]
    check("bank statements: full coverage becomes EVIDENCED", bank["status"] == "EVIDENCED" and bank["evidence"][0]["detail"]["coverage"] == 1.0, str(bank["evidence"][0]["detail"]))

    comp.set_profile("Acme Traders", "regular_monthly", None, CO_GSTIN, "alice")
    comp.generate_tasks("Acme Traders", 2025, "alice")
    comp.generate_tasks("Acme Traders", 2026, "alice")
    gst = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["gst_return_history"]
    check("GST history: nothing recorded -> PARTIAL listing the unrecorded returns", gst["status"] == "PARTIAL" and gst["evidence"][0]["detail"]["recorded"] == 0 and gst["evidence"][0]["detail"]["due"] >= 20)
    start = (today - timedelta(days=365)).isoformat()
    db.sql('UPDATE "MSME Compliance Task" SET status = \'filing_recorded\' WHERE company = ? AND due_date >= ? AND due_date <= ? '
           "AND form IN ('GSTR-1','GSTR-3B')", ["Acme Traders", start, today.isoformat()]); db.commit()
    gst = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["gst_return_history"]
    check("GST history: all due returns recorded -> USER_RECORDED, explicitly unverified",
          gst["status"] == "USER_RECORDED" and "unverified" in gst["evidence"][0]["ref"], str(gst["evidence"][0]["detail"]["unrecorded"]))
    db.sql('UPDATE "MSME Compliance Task" SET status = \'open\' WHERE id IN (SELECT id FROM "MSME Compliance Task" '
           "WHERE company = ? AND form = 'GSTR-3B' AND due_date >= ? AND due_date <= ? ORDER BY due_date LIMIT 2)",
           ["Acme Traders", start, today.isoformat()]); db.commit()
    gst = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["gst_return_history"]
    check("GST history: two unrecorded returns -> PARTIAL naming them", gst["status"] == "PARTIAL" and gst["evidence"][0]["detail"]["unrecorded_count"] == 2)

    # sales invoice evidence from real approved documents
    n = [0]
    cur_vals = {}

    class Fake:
        name, version = "fake", "1"

        def extract(self, path):
            fields = [ExtractedField(p, v, 0.97, 1, v, {"x0": .1, "y0": .1, "x1": .2, "y1": .2}, "verified") for p, v in cur_vals.items()]
            return ExtractionOutcome("extracted", "invoice", fields, [], extractor="fake", extractor_version="1")

    docs = DocumentService(Fake(), LocalDocumentStorage(storage_dir))

    def invoice(month):
        n[0] += 1
        cur_vals.clear()
        cur_vals.update({"invoice_number": f"CR-{n[0]}", "issue_date": f"{month}-10", "seller.name": "Acme",
                         "seller.tax_ids[0].value": CO_GSTIN, "seller.tax_ids[0].scheme": "gst",
                         "buyer.tax_ids[0].value": OTHER, "buyer.tax_ids[0].scheme": "gst",
                         "subtotal": "1000.00", "tax_amount": "180.00", "total_amount": "1180.00"})
        d, _ = docs.ingest("u", f"c{n[0]}.pdf", f"%PDF-1.4\n% credit {n[0]}\n%%EOF\n".encode(), "application/pdf")
        docs.process(d["id"]); docs.approve(d["id"], "reviewer")
        return d["id"]

    inv = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["sales_invoice_evidence"]
    check("no approved invoices -> MISSING", inv["status"] == "MISSING")
    for m in ("2025-10", "2025-11", "2025-12"):
        invoice(m)
    inv = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["sales_invoice_evidence"]
    check("3 of 12 months evidenced -> PARTIAL naming the 9 missing months",
          inv["status"] == "PARTIAL" and len(inv["evidence"][0]["detail"]["missing_months"]) == 9 and "2026-09" in inv["what_is_missing"], str(inv["what_is_missing"]))
    first_doc = db.sql('SELECT id FROM "MSME Document" ORDER BY created_at LIMIT 1')[0]["id"]
    for m in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"):
        invoice(m)
    inv = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["sales_invoice_evidence"]
    check("all 12 months evidenced -> EVIDENCED", inv["status"] == "EVIDENCED", str(inv["what_is_missing"]))

    # udyam + attestations
    items = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}
    check("Udyam entered by the user is USER_ATTESTED and says it is not verified",
          items["udyam_registration"]["status"] == "USER_ATTESTED" and "registration not verified" in items["udyam_registration"]["evidence"][0]["detail"])
    check("platform-evidenced items cannot be self-attested", raises("not_attestable", svc.attest, "Acme Traders", "bank_statements", "provided", "x", None, "alice"))
    check("attestation needs a valid status and a note", raises("invalid_status", svc.attest, "Acme Traders", "financial_statements", "done", "x", None, "alice")
          and raises("note_required", svc.attest, "Acme Traders", "financial_statements", "provided", " ", None, "alice"))
    check("attestation with an unknown document is rejected", raises("unknown_document", svc.attest, "Acme Traders", "financial_statements", "provided", "FY25 accounts", "MDOC-ffffffffffff", "alice"))
    att = svc.attest("Acme Traders", "financial_statements", "provided", "FY2025-26 balance sheet and P&L from our CA", first_doc, "alice")
    check("attestation is stored as unverified", att["verified"] is False)
    fs = {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["financial_statements"]
    check("attested item shows the user and the linked document, status USER_ATTESTED",
          fs["status"] == "USER_ATTESTED" and {e["type"] for e in fs["evidence"]} == {"attestation", "document"})
    svc.attest("Acme Traders", "income_tax_returns", "not_applicable", "Newly formed; no return due yet", None, "alice")
    check("not_applicable is recorded as such", {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["income_tax_returns"]["status"] == "NOT_APPLICABLE")
    now["t"] += timedelta(seconds=1)
    svc.attest("Acme Traders", "income_tax_returns", "provided", "Returns for FY23-25 obtained", None, "bob")
    check("the newest attestation wins", {i["key"]: i for i in svc.readiness("Acme Traders")["items"]}["income_tax_returns"]["status"] == "USER_ATTESTED")

    # ---------------- summary and PDF ----------------
    svc.set_fact("Acme Traders", "loan_purpose", "<script>alert(1)</script><img src=\"http://127.0.0.1:9/x.png\"> working capital", "alice")
    data = svc.application_summary("Acme Traders", "cgtmse")
    check("summary carries facts, assessment, classification and readiness", data["assessment"]["scheme_id"] == "cgtmse" and data["facts"] and data["readiness"]["items"])
    check("unknown scheme -> 404", raises("unknown_scheme", svc.application_summary, "Acme Traders", "nope"))
    pdf = report.summary_pdf(data)
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = " ".join(" ".join((p.extract_text() or "").split()) for p in doc.pages)
    check("PDF states it is not an application and not a lender decision", pdf[:5] == b"%PDF-" and "NOT AN APPLICATION AND NOT A LENDER DECISION" in text, text[:150])
    check("PDF shows rule version, verification status and the unmodelled details",
          "cgtmse-revamp-2023-04-01" in text and "SECONDARY_ONLY" in text and "deliberately not modelled" in text)
    check("PDF shows evidence sources and unverified-typical documents", "user_declared" in text and "UNVERIFIED_TYPICAL" in text)
    html_text = report.summary_html(data)
    check("hostile fact text is escaped in the report", "<script>alert(1)</script>" not in html_text and "&lt;script&gt;" in html_text and "<img src" not in html_text)

    # ---------------- concurrency + audit ----------------
    errs = []

    def race(i):
        try:
            svc.set_fact("Race Co", ("udyam_registered", "sector", "credit_facility_amount", "turnover_inr")[i % 4],
                         (True, "trading", 100000 + i, 1000000 + i)[i % 4], f"racer{i}")
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    ts = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    v = comp.verify_audit()
    check("8 concurrent fact writes succeed and the audit chain stays valid", not errs and v["ok"] and v["head"]["seq"] == v["events"], f"{errs} {v}")
    actions = {e["action"] for e in comp.audit_events(limit=500)}
    check("facts, attestations and assessments are all in the audit trail", {"credit.fact_set", "credit.attest", "scheme.assess"} <= actions)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
