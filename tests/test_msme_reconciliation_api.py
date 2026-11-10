#!/usr/bin/env python3
"""HTTP-level tests for /api/msme/reconciliation and /api/msme/credit/turnover-check.

Run:  python -m tests.test_msme_reconciliation_api      (LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)
"""

import os
import sys
import tempfile
import warnings

sys.dont_write_bytecode = True
CO_GSTIN, OTHER = "27AAPFU0939F1ZV", "09AAACT2727Q1ZU"


def main() -> int:
    warnings.filterwarnings("ignore")
    from tests._msme_db import assert_backend, reset_test_db

    db_path = reset_test_db("lambda_msme_recon_api_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_recon_api_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-recon-api", "OPENAI_API_KEY": "synthetic-not-used"})

    from fastapi.testclient import TestClient
    from api.main import app
    from lambda_erp.database import get_db
    from msme.documents.extraction import ExtractedField, ExtractionOutcome
    from msme.documents.service import DocumentService
    from msme.documents.storage import LocalDocumentStorage

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    base, co = "/api/msme/reconciliation", "Acme%20Traders"
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

        db = get_db()
        db.insert("Company", {"name": "Acme Traders", "company_name": "Acme Traders", "default_currency": "INR"})
        db.insert("Account", {"name": "Bank - A", "account_name": "Bank", "company": "Acme Traders", "root_type": "Asset", "report_type": "Balance Sheet", "is_group": 0})
        db.insert("Bank Account", {"name": "BA-A", "account_name": "A", "company": "Acme Traders", "account": "Bank - A", "iban": "IN00SYNTH0000000009", "currency": "INR"})
        db.insert("Bank Transaction", {"name": "T1", "bank_account": "Bank - A", "bank_account_id": "BA-A", "posting_date": "2026-06-25", "deposit": 1180,
                                       "withdrawal": 0, "currency": "INR", "description": "NEFT KURNOOL TRADERS INV20260042", "counterparty_name": "KURNOOL TRADERS",
                                       "status": "Unreconciled", "reversal_indicator": 0, "docstatus": 1})
        db.insert("Bank Transaction", {"name": "T2", "bank_account": "Bank - A", "bank_account_id": "BA-A", "posting_date": "2026-06-26", "deposit": 777,
                                       "withdrawal": 0, "currency": "INR", "description": "UPI MISC", "counterparty_name": "UNKNOWN",
                                       "status": "Unreconciled", "reversal_indicator": 0, "docstatus": 1})
        db.commit()
        cur = {}

        class Fake:
            name, version = "fake", "1"

            def extract(self, path):
                fields = [ExtractedField(p, v, 0.97, 1, v, {"x0": .1, "y0": .1, "x1": .2, "y1": .2}, "verified") for p, v in cur.items()]
                return ExtractionOutcome("extracted", "invoice", fields, [], extractor="fake", extractor_version="1")

        svc = DocumentService(Fake(), LocalDocumentStorage(storage_dir))

        def invoice(number, issued, subtotal, tax, party, tag):
            cur.clear()
            cur.update({"invoice_number": number, "issue_date": issued, "subtotal": subtotal, "tax_amount": tax,
                        "total_amount": f"{float(subtotal) + float(tax):.2f}", "currency": "INR", "seller.name": "Acme Traders", "buyer.name": party,
                        "seller.tax_ids[0].value": CO_GSTIN, "seller.tax_ids[0].scheme": "gst",
                        "buyer.tax_ids[0].value": OTHER, "buyer.tax_ids[0].scheme": "gst", "due_date": "2026-07-10"})
            d, _ = svc.ingest("u", f"{tag}.pdf", f"%PDF-1.4\n% {tag}\n%%EOF\n".encode(), "application/pdf")
            svc.process(d["id"]); svc.approve(d["id"], "reviewer")
            return d["id"]

        doc_a = invoice("INV-2026-0042", "2026-06-10", "1000.00", "180.00", "Kurnool Traders Pvt Ltd", "a")
        doc_b = invoice("INV-2026-0077", "2026-06-11", "400.00", "100.00", "Gamma Stores", "b")

        # ---- authorization
        for path in (f"/matches?company={co}", f"/unmatched/documents?company={co}", f"/unmatched/transactions?company={co}",
                     f"/summary?company={co}", "/matches/MRCM-000000000000"):
            check(f"anonymous cannot GET {path.split('?')[0]}", anon.get(base + path).status_code == 401)
        check("anonymous cannot reach the turnover check", anon.get(f"/api/msme/credit/turnover-check?company={co}").status_code == 401)
        check("viewer cannot run the matcher", viewer.post(base + "/run", json={"company": "Acme Traders"}).status_code == 403)
        for label, call in (("confirm", lambda: viewer.post(base + "/matches/MRCM-000000000000/confirm", json={})),
                            ("reject", lambda: viewer.post(base + "/matches/MRCM-000000000000/reject", json={"reason": "x"})),
                            ("unmatch", lambda: viewer.post(base + "/matches/MRCM-000000000000/unmatch", json={"reason": "x"})),
                            ("match manually", lambda: viewer.post(base + "/matches/manual", json={"company": "Acme Traders", "document_id": doc_a, "bank_transaction": "T1", "note": "x"}))):
            check(f"viewer cannot {label}", call().status_code == 403)

        # ---- run needs a profile with a GSTIN
        r = mgr.post(base + "/run", json={"company": "Acme Traders"})
        check("run without a compliance profile -> 409 profile_required", r.status_code == 409 and r.json()["detail"]["code"] == "profile_required", r.text[:120])
        assert mgr.put("/api/msme/compliance/profile", json={"company": "Acme Traders", "filer_type": "regular_monthly", "gstin": CO_GSTIN}).status_code == 200
        r = mgr.post(base + "/run", json={"company": "Acme Traders"})
        check("manager runs the matcher", r.status_code == 200 and r.json()["suggested"] == 1 and r.json()["strong"] == 1 and "heuristic" in r.json()["note"], r.text[:200])

        # ---- reading matches
        lst = viewer.get(f"{base}/matches?company={co}").json()
        m = lst["rows"][0]
        check("viewer lists matches with named reasons and both sides", lst["total"] == 1 and m["status"] == "suggested" and m["classification"] == "STRONG"
              and {x["code"] for x in m["reasons"]} >= {"AMOUNT_EXACT", "REFERENCE"} and m["document"]["id"] == doc_a and m["bank_transaction"]["name"] == "T1", str(m))
        check("match ids are deterministic (stable across re-runs)", mgr.post(base + "/run", json={"company": "Acme Traders"}).status_code == 200
              and viewer.get(f"{base}/matches?company={co}").json()["rows"][0]["id"] == m["id"])
        check("status filter and absurd limit", viewer.get(f"{base}/matches?company={co}&status=confirmed").json()["total"] == 0
              and viewer.get(f"{base}/matches?company={co}&limit=9999").status_code == 422)
        check("malformed match id -> 422, unknown -> 404", viewer.get(base + "/matches/not-an-id").status_code == 422
              and viewer.get(base + "/matches/MRCM-000000000000").json()["detail"]["code"] == "not_found")

        # ---- review over HTTP
        r = mgr.post(f"{base}/matches/{m['id']}/reject", json={"reason": ""})
        check("reject without a reason -> 422", r.status_code == 422)
        r = mgr.post(f"{base}/matches/{m['id']}/confirm", json={"note": "Matches the statement line"})
        check("manager confirms", r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["decided_by"], r.text[:150])
        r = mgr.post(f"{base}/matches/{m['id']}/confirm", json={})
        check("confirming twice -> 409 not_confirmable", r.status_code == 409 and r.json()["detail"]["code"] == "not_confirmable")
        r = mgr.post(base + "/matches/manual", json={"company": "Acme Traders", "document_id": "not-a-doc", "bank_transaction": "T2", "note": "x"})
        check("manual match validates the document id format -> 422", r.status_code == 422)
        r = mgr.post(base + "/matches/manual", json={"company": "Acme Traders", "document_id": doc_a, "bank_transaction": "T2", "note": "x"})
        check("manual match on an already-matched invoice -> 409", r.status_code == 409 and r.json()["detail"]["code"] == "already_matched", r.text[:150])
        r = mgr.post(base + "/matches/manual", json={"company": "Acme Traders", "document_id": doc_b, "bank_transaction": "T2", "note": "Customer confirmed by phone"})
        check("manual match is created confirmed, classified MANUAL, with the amount difference recorded",
              r.status_code == 201 and r.json()["classification"] == "MANUAL" and r.json()["status"] == "confirmed"
              and r.json()["difference"] == "277.00", r.text[:200])
        r = mgr.post(f"{base}/matches/{m['id']}/unmatch", json={"reason": "Wrong customer"})
        check("unmatch reverses a confirmation with its reason", r.status_code == 200 and r.json()["status"] == "rejected" and "Wrong customer" in r.json()["note"])

        # ---- queues and summary
        ud = viewer.get(f"{base}/unmatched/documents?company={co}").json()
        check("unmatched invoices include the one whose match was reversed", doc_a in {x["id"] for x in ud["rows"]} and doc_b not in {x["id"] for x in ud["rows"]}, str(ud))
        check("unmatched queue kinds are validated by the route", viewer.get(f"{base}/unmatched/bogus?company={co}").status_code == 422)
        ut = viewer.get(f"{base}/unmatched/transactions?company={co}").json()
        check("unmatched transactions list the payment that is free again", {x["name"] for x in ut["rows"]} == {"T1"}, str(ut))
        s = viewer.get(f"{base}/summary?company={co}").json()
        check("summary reports matched and unmatched value", s["invoices"]["outward"]["matched"] == 1 and s["invoices"]["outward"]["unmatched"] == 1, str(s["invoices"]))
        check("unknown company reads are rejected cleanly", viewer.get(f"{base}/summary?company=Nope").status_code in (404, 409))

        # ---- audit chain covers it
        v = viewer.get("/api/msme/compliance/audit/verify").json()
        evs = {e["action"] for e in viewer.get("/api/msme/compliance/audit", params={"limit": 500}).json()["events"]}
        check("the audit chain is valid and includes the reconciliation actions", v["ok"] and {"recon.run", "recon.confirm", "recon.manual", "recon.unmatch"} <= evs, str(evs))

        # ---- turnover check
        check("turnover check without a declaration says so", viewer.get(f"/api/msme/credit/turnover-check?company={co}").json()["status"] == "NO_DECLARATION")
        assert mgr.put("/api/msme/credit/facts", json={"company": "Acme Traders", "key": "turnover_inr", "value": 9000}).status_code == 200
        t = viewer.get(f"/api/msme/credit/turnover-check?company={co}").json()
        check("turnover check reports totals, caveats and only heuristic flags",
              t["status"] == "CHECKED" and t["documented_outward_taxable"] == "1400.00" and t["bank_deposits"] == "1957.00"
              and len(t["caveats"]) == 3 and all(f["severity"] in ("HEURISTIC", "INFO") for f in t["flags"]), str(t))
        check("turnover check on an unknown company -> 404", viewer.get("/api/msme/credit/turnover-check?company=Nope").status_code == 404)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
