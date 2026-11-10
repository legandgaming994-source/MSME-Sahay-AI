#!/usr/bin/env python3
"""HTTP-level tests for /api/msme/credit: roles, validation, facts, readiness, assessments, summary PDF.

Run:  python -m tests.test_msme_credit_api      (LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)
"""

import os
import sys
import warnings

sys.dont_write_bytecode = True


def main() -> int:
    warnings.filterwarnings("ignore")
    from tests._msme_db import assert_backend, reset_test_db

    db_path = reset_test_db("lambda_msme_credit_api_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-credit-api", "OPENAI_API_KEY": "synthetic-not-used"})

    from fastapi.testclient import TestClient
    from api.main import app
    from lambda_erp.database import get_db

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    base, co = "/api/msme/credit", "Acme%20Traders"
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
        db.commit()

        # ---- authorization
        for path in (f"/facts?company={co}", f"/readiness?company={co}", "/schemes", f"/schemes/assessments?company={co}",
                     f"/schemes/cgtmse/summary?company={co}", f"/schemes/cgtmse/summary.pdf?company={co}", f"/facts/history?company={co}&key=sector"):
            check(f"anonymous cannot GET {path.split('?')[0]}", anon.get(base + path).status_code == 401)
        check("viewer cannot enter facts", viewer.put(base + "/facts", json={"company": "Acme Traders", "key": "sector", "value": "service"}).status_code == 403)
        check("viewer cannot attest", viewer.put(base + "/readiness/attestations", json={"company": "Acme Traders", "item_key": "financial_statements", "status": "provided", "note": "x"}).status_code == 403)
        check("viewer cannot run a persisted assessment", viewer.post(base + "/schemes/assess", json={"company": "Acme Traders"}).status_code == 403)
        check("viewer can read facts and the scheme catalogue", viewer.get(f"{base}/facts?company={co}").status_code == 200 and viewer.get(base + "/schemes").status_code == 200)

        # ---- facts
        F = lambda key, value, **kw: mgr.put(base + "/facts", json={"company": "Acme Traders", "key": key, "value": value, **kw})  # noqa: E731
        check("unknown company -> 404", mgr.put(base + "/facts", json={"company": "Nope", "key": "sector", "value": "service"}).json()["detail"]["code"] == "company_not_found")
        r = F("msme_category", "Micro")
        check("derived fact cannot be set -> 422", r.status_code == 422 and r.json()["detail"]["code"] == "derived_fact")
        r = F("udyam_number", "UDYAM-XX-1")
        check("invalid Udyam -> 422 invalid_udyam", r.status_code == 422 and r.json()["detail"]["code"] == "invalid_udyam", r.text[:100])
        check("invalid evidence id pattern -> 422", F("turnover_inr", 1, evidence_document_id="not-an-id").status_code == 422)
        check("unknown evidence document -> 422", F("turnover_inr", 1, evidence_document_id="MDOC-ffffffffffff").json()["detail"]["code"] == "unknown_document")
        for k, v in (("udyam_number", "udyam-mh-27-0012345"), ("udyam_registered", True), ("sector", "manufacturing"), ("non_corporate_non_farm", True),
                     ("investment_inr", 20000000), ("turnover_inr", 80000000), ("credit_facility_amount", 800000), ("collateral_free_requested", True)):
            r = F(k, v)
            check(f"manager sets {k}", r.status_code == 200 and r.json()["source"] == "user_declared", r.text[:100])
        g = viewer.get(f"{base}/facts?company={co}").json()
        check("facts response lists values, derived category and the settable registry",
              len(g["facts"]) == 8 and g["derived"][0]["value"] == "Micro" and "turnover_inr" in g["settable"], str(g["derived"]))
        check("repeat value reports unchanged", F("sector", "manufacturing").json()["unchanged"] is True)
        F("credit_facility_amount", 900000)
        h = viewer.get(f"{base}/facts/history?company={co}&key=credit_facility_amount").json()["history"]
        check("history endpoint shows both values in order", [x["value"] for x in h] == [800000, 900000])

        # ---- readiness
        r = viewer.get(f"{base}/readiness?company={co}").json()
        st = {i["key"]: i["status"] for i in r["items"]}
        check("readiness: Udyam attested, bank statements missing, requirements labelled unverified",
              st["udyam_registration"] == "USER_ATTESTED" and st["bank_statements"] == "MISSING" and r["requirement_basis"] == "UNVERIFIED_TYPICAL")
        check("platform-evidenced item cannot be attested -> 422", mgr.put(base + "/readiness/attestations", json={"company": "Acme Traders", "item_key": "bank_statements", "status": "provided", "note": "trust me"}).status_code == 422)
        r = mgr.put(base + "/readiness/attestations", json={"company": "Acme Traders", "item_key": "kyc_and_business_proof", "status": "provided", "note": "KYC pack ready"})
        check("manager attests an item (unverified)", r.status_code == 200 and r.json()["verified"] is False)
        check("attested item now shows USER_ATTESTED", {i["key"]: i["status"] for i in viewer.get(f"{base}/readiness?company={co}").json()["items"]}["kyc_and_business_proof"] == "USER_ATTESTED")

        # ---- schemes
        cat = viewer.get(base + "/schemes").json()["schemes"]
        check("catalogue lists both schemes with source verification", {s["id"] for s in cat} == {"pmmy-mudra", "cgtmse"} and all(s["source"]["verification"] for s in cat))
        r = mgr.post(base + "/schemes/assess", json={"company": "Acme Traders"})
        a = {x["scheme_id"]: x for x in r.json()["assessments"]}
        check("assessment persisted for both schemes", r.status_code == 200 and set(r.json()["snapshot_ids"]) == {"pmmy-mudra", "cgtmse"})
        check("CGTMSE: Micro + manufacturing + registered + collateral-free -> criteria appear met", a["cgtmse"]["status"] == "CRITERIA_APPEARS_MET", a["cgtmse"]["status"])
        check("PMMY 9 lakh Kishor/Tarun range -> criteria appear met without a Tarun-Plus prerequisite", a["pmmy-mudra"]["status"] == "CRITERIA_APPEARS_MET", str(a["pmmy-mudra"]["criteria"]))
        check("every result requires a lender decision", all(x["lender_decision_required"] for x in a.values()))
        hist = viewer.get(f"{base}/schemes/assessments?company={co}&scheme_id=cgtmse").json()["assessments"]
        check("history returns the stored snapshot", len(hist) == 1 and hist[0]["rule_version"] == "cgtmse-revamp-2023-04-01" and hist[0]["result"]["status"] == "CRITERIA_APPEARS_MET")
        check("absurd limit -> 422", viewer.get(f"{base}/schemes/assessments?company={co}&limit=9999").status_code == 422)

        # ---- summary / pdf
        s = viewer.get(f"{base}/schemes/cgtmse/summary?company={co}").json()
        check("summary JSON carries facts, assessment, readiness and classification", s["assessment"]["scheme_id"] == "cgtmse" and s["facts"] and s["readiness"]["items"] and s["classification"]["category"] == "Micro")
        r = viewer.get(f"{base}/schemes/cgtmse/summary.pdf?company={co}")
        check("PDF summary downloads with safe headers", r.status_code == 200 and r.content[:5] == b"%PDF-" and r.headers["content-type"] == "application/pdf"
              and "attachment" in r.headers["content-disposition"] and r.headers["x-content-type-options"] == "nosniff", str(dict(r.headers)))
        check("unknown scheme -> 404", viewer.get(f"{base}/schemes/nope/summary?company={co}").json()["detail"]["code"] == "unknown_scheme")
        check("malformed scheme id -> 422", viewer.get(f"{base}/schemes/NOT%20OK/summary?company={co}").status_code == 422)
        check("unknown company on read -> 404", viewer.get(f"{base}/readiness?company=Nope").status_code == 404)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
