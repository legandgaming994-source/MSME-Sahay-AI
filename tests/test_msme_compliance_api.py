#!/usr/bin/env python3
"""HTTP-level tests for /api/msme/compliance: roles, maker-checker over the wire, reports, validation.

Run:  python -m tests.test_msme_compliance_api      (LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)
"""

import os
import sys
import warnings

sys.dont_write_bytecode = True


def main() -> int:
    warnings.filterwarnings("ignore")
    from tests._msme_db import assert_backend, reset_test_db

    db_path = reset_test_db("lambda_msme_comp_api_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-comp-api", "OPENAI_API_KEY": "synthetic-not-used"})
    os.environ.pop("MSME_REQUIRE_DISTINCT_APPROVER", None)

    from fastapi.testclient import TestClient
    from api.main import app
    from lambda_erp.database import get_db

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    base = "/api/msme/compliance"
    with TestClient(app, raise_server_exceptions=False) as admin:
        assert_backend(bool(os.environ.get("LAMBDA_ERP_TEST_DB")))
        assert admin.post("/api/auth/register", json={"email": "a@x.invalid", "full_name": "A", "password": "synthetic-pass-1"}).status_code == 200
        admin.put("/api/auth/settings", json={"allow_public_signup": "1"})
        viewer = TestClient(app, raise_server_exceptions=False)
        assert viewer.post("/api/auth/register", json={"email": "v@x.invalid", "full_name": "V", "password": "synthetic-pass-2"}).status_code == 200

        def manager(email):
            tok = admin.post("/api/auth/invite", json={"email": email, "role": "manager"}).json()["token"]
            c = TestClient(app, raise_server_exceptions=False)
            assert c.post("/api/auth/register", json={"email": email, "full_name": email, "password": "synthetic-pass-3", "invite_token": tok}).status_code == 200
            return c

        m1, m2, anon = manager("m1@x.invalid"), manager("m2@x.invalid"), TestClient(app, raise_server_exceptions=False)
        db = get_db()
        db.insert("Company", {"name": "Acme Traders", "company_name": "Acme Traders", "default_currency": "INR"})
        db.commit()

        # ---- authorization
        for path in ("/profile?company=Acme%20Traders", "/tasks", "/upcoming?company=Acme%20Traders", "/audit", "/audit/verify",
                     "/calendar.csv?company=Acme%20Traders&fy_start_year=2026"):
            check(f"anonymous cannot GET {path.split('?')[0]}", anon.get(base + path).status_code == 401)
        prof = {"company": "Acme Traders", "filer_type": "regular_monthly", "gstin": "27AAPFU0939F1ZV"}
        check("viewer cannot set the profile", viewer.put(base + "/profile", json=prof).status_code == 403)
        check("viewer cannot generate tasks", viewer.post(base + "/tasks/generate", json={"company": "Acme Traders", "fy_start_year": 2026}).status_code == 403)
        check("viewer cannot add overrides", viewer.post(base + "/overrides", json={"company": "Acme Traders", "form": "GSTR-3B", "period": "2026-06", "due_date": "2026-07-31", "source": "notification x"}).status_code == 403)
        for label, call in (("open a workpaper", lambda: viewer.post(base + "/tasks/MTSK-000000000000/workpapers")),
                            ("edit a workpaper", lambda: viewer.put(base + "/workpapers/MWP-000000000000", json={"notes": "x"})),
                            ("submit", lambda: viewer.post(base + "/workpapers/MWP-000000000000/submit")),
                            ("approve", lambda: viewer.post(base + "/workpapers/MWP-000000000000/approve")),
                            ("reject", lambda: viewer.post(base + "/workpapers/MWP-000000000000/reject", json={"reason": "x"})),
                            ("record a filing", lambda: viewer.post(base + "/tasks/MTSK-000000000000/filing", json={"reference": "AB1234", "filed_on": "2026-01-01"}))):
            check(f"viewer cannot {label}", call().status_code == 403)
        check("viewer can read profile status (404 before setup)", viewer.get(base + "/profile?company=Acme%20Traders").status_code == 404)

        # ---- profile validation + generation
        r = m1.put(base + "/profile", json=dict(prof, state_category="Z"))
        check("invalid state category -> 422", r.status_code == 422 and r.json()["detail"]["code"] == "invalid_state_category", r.text[:120])
        r = m1.put(base + "/profile", json={"company": "Acme Traders", "filer_type": "qrmp"})
        check("QRMP without category -> 422", r.status_code == 422 and r.json()["detail"]["code"] == "state_category_required")
        r = m1.put(base + "/profile", json=dict(prof, gstin="27AAPFU0939F1ZW"))
        check("bad GSTIN check character -> 422", r.status_code == 422 and r.json()["detail"]["code"] == "invalid_gstin")
        check("manager sets profile", m1.put(base + "/profile", json=prof).status_code == 200)
        check("viewer reads the profile", viewer.get(base + "/profile?company=Acme%20Traders").json()["gstin"] == "27AAPFU0939F1ZV")
        check("generation year out of range -> 422", m1.post(base + "/tasks/generate", json={"company": "Acme Traders", "fy_start_year": 1999}).status_code == 422)
        r = m1.post(base + "/tasks/generate", json={"company": "Acme Traders", "fy_start_year": 2026})
        check("manager generates the year's tasks", r.status_code == 200 and r.json()["created"] == 25, r.text[:150])
        r = m1.post(base + "/overrides", json={"company": "Acme Traders", "form": "GSTR-3B", "period": "2026-06", "due_date": "2026-07-31", "source": "x"})
        check("override with a trivial source -> 422", r.status_code == 422)

        lst = viewer.get(base + "/tasks", params={"company": "Acme Traders", "fy_start_year": 2026, "limit": 5}).json()
        check("viewer lists tasks with pagination", lst["total"] == 25 and len(lst["rows"]) == 5)
        check("absurd limit -> 422", viewer.get(base + "/tasks", params={"limit": 100000}).status_code == 422)
        r = viewer.get(base + "/calendar.csv", params={"company": "Acme Traders", "fy_start_year": 2026})
        check("calendar csv download is an attachment with nosniff", r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
              and r.headers["x-content-type-options"] == "nosniff" and len(r.text.strip().splitlines()) == 26, r.text[:100])
        up = viewer.get(base + "/upcoming", params={"company": "Acme Traders", "days": 30})
        check("upcoming returns a task list", up.status_code == 200 and isinstance(up.json()["tasks"], list), up.text[:100])
        check("upcoming horizon is bounded (days<=366)", viewer.get(base + "/upcoming", params={"company": "Acme Traders", "days": 400}).status_code == 422)

        # ---- bad ids
        check("malformed task id -> 422", viewer.get(base + "/tasks/not-an-id").status_code == 422)
        r = viewer.get(base + "/tasks/MTSK-000000000000")
        check("unknown task -> 404 with code", r.status_code == 404 and r.json()["detail"]["code"] == "not_found")

        # ---- two-person approval over HTTP
        task = lst["rows"][0]
        tid = task["id"]
        r = m1.post(f"{base}/tasks/{tid}/workpapers")
        check("manager opens a workpaper (201)", r.status_code == 201 and r.json()["status"] == "draft", r.text[:150])
        wid = r.json()["id"]
        check("viewer can read the workpaper", viewer.get(f"{base}/workpapers/{wid}").json()["content"]["checklist_source"] == "INTERNAL_GUIDANCE")
        r = m1.post(f"{base}/workpapers/{wid}/submit")
        check("submit with pending required items -> 409", r.status_code == 409 and r.json()["detail"]["code"] == "checklist_incomplete")
        req = [i["key"] for i in viewer.get(f"{base}/workpapers/{wid}").json()["content"]["checklist"] if i["required"]]
        r = m1.put(f"{base}/workpapers/{wid}", json={"checklist": [{"key": k, "status": "done"} for k in req], "notes": "ok"})
        check("manager completes the checklist", r.status_code == 200)
        r = m1.put(f"{base}/workpapers/{wid}", json={"checklist": [{"key": "nope", "status": "done"}]})
        check("unknown item -> 422", r.status_code == 422 and r.json()["detail"]["code"] == "unknown_item")
        check("submit succeeds", m1.post(f"{base}/workpapers/{wid}/submit").json()["status"] == "submitted")
        r = m1.post(f"{base}/workpapers/{wid}/approve")
        check("preparer cannot approve (403 maker_checker)", r.status_code == 403 and r.json()["detail"]["code"] == "maker_checker", r.text[:120])
        check("reject requires a reason (422)", m2.post(f"{base}/workpapers/{wid}/reject", json={"reason": ""}).status_code == 422)
        r = m2.post(f"{base}/workpapers/{wid}/approve")
        check("a second manager approves", r.status_code == 200 and r.json()["status"] == "approved" and r.json()["decided_by"], r.text[:150])
        r = viewer.get(f"{base}/workpapers/{wid}/report.pdf")
        check("PDF report downloads with safe headers", r.status_code == 200 and r.content[:5] == b"%PDF-"
              and r.headers["content-type"] == "application/pdf" and r.headers["x-content-type-options"] == "nosniff"
              and "attachment" in r.headers["content-disposition"], str(dict(r.headers)))
        r = m2.post(f"{base}/tasks/{tid}/filing", json={"reference": "<script>", "filed_on": "2026-01-01"})
        check("hostile filing reference -> 422", r.status_code == 422 and r.json()["detail"]["code"] == "invalid_reference")
        r = m2.post(f"{base}/tasks/{tid}/filing", json={"reference": "AA2706260001", "filed_on": "2026-01-01", "note": "from portal"})
        check("filing recorded with the unverified label", r.status_code == 200 and r.json()["status"] == "filing_recorded"
              and "not verified" in r.json()["filing_note_label"].lower(), r.text[:200])
        check("recording twice -> 409", m2.post(f"{base}/tasks/{tid}/filing", json={"reference": "AA2706260002", "filed_on": "2026-01-01"}).status_code == 409)

        # ---- audit
        ev = viewer.get(base + "/audit", params={"entity_id": wid}).json()["events"]
        check("audit trail lists the workpaper events", {e["action"] for e in ev} >= {"workpaper.open", "workpaper.update", "workpaper.submit", "workpaper.approve"})
        v = viewer.get(base + "/audit/verify").json()
        check("audit chain verifies and reports its head", v["ok"] and v["head"]["seq"] == v["events"] > 5, str(v))
        check("the task detail includes its audit trail", any(e["action"] == "filing.recorded_by_user" for e in viewer.get(f"{base}/tasks/{tid}").json()["audit"]))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
