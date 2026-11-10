#!/usr/bin/env python3
"""HTTP-level tests for /api/msme/documents: roles, upload->process->review->approve, hostile input.

The service runs with a scripted extractor (no OCR/LLM) so this isolates routing, authorization,
validation and serialization. Real-engine behaviour is covered by test_msme_docket_integration.

Run:  python -m tests.test_msme_documents_api
"""

import os
import sys
import tempfile
import warnings

sys.dont_write_bytecode = True


def main() -> int:
    warnings.filterwarnings("ignore")
    from tests._msme_db import reset_test_db

    db_path = reset_test_db("lambda_msme_api_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_api_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-api", "OPENAI_API_KEY": "synthetic-not-used"})

    from fastapi.testclient import TestClient
    from api.main import app
    from api.routers.msme_documents import get_service
    from msme.documents.extraction import ExtractedField, ExtractionOutcome
    from msme.documents.service import DocumentService
    from msme.documents.storage import LocalDocumentStorage

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    n = [0]

    def pdf() -> bytes:
        n[0] += 1
        return f"%PDF-1.4\n% api {n[0]}\n1 0 obj\n<< >>\nendobj\n%%EOF\n".encode()

    BASE = {"issue_date": "2026-09-01", "seller.name": "Northgate Supplies", "subtotal": "1000",
            "tax_amount": "180", "total_amount": "1180"}
    current = {"values": None}

    class Fake:
        name, version = "fake", "1"

        def extract(self, path):
            n[0] += 1
            values = current["values"] or dict(BASE, invoice_number=f"INV-API-{n[0]}")
            fields = [ExtractedField(p, v, 0.97, 1, v, {"x0": 0.1, "y0": 0.1, "x1": 0.3, "y1": 0.2}, "verified")
                      for p, v in values.items()]
            return ExtractionOutcome("extracted", "invoice", fields, [], extractor="fake", extractor_version="1")

    app.dependency_overrides[get_service] = lambda: DocumentService(Fake(), LocalDocumentStorage(storage_dir))

    def up(client, data=None, name="inv.pdf", mime="application/pdf", **params):
        return client.post("/api/msme/documents", params=params, files={"file": (name, data or pdf(), mime)})

    with TestClient(app, raise_server_exceptions=False) as admin:
        from tests._msme_db import assert_backend

        assert_backend(bool(os.environ.get("LAMBDA_ERP_TEST_DB")))
        assert admin.post("/api/auth/register", json={"email": "a@x.invalid", "full_name": "A", "password": "synthetic-pass-1"}).status_code == 200
        admin.put("/api/auth/settings", json={"allow_public_signup": "1"})
        viewer = TestClient(app, raise_server_exceptions=False)
        assert viewer.post("/api/auth/register", json={"email": "v@x.invalid", "full_name": "V", "password": "synthetic-pass-2"}).status_code == 200
        inv = admin.post("/api/auth/invite", json={"email": "m@x.invalid", "role": "manager"}).json()["token"]
        mgr = TestClient(app, raise_server_exceptions=False)
        assert mgr.post("/api/auth/register", json={"email": "m@x.invalid", "full_name": "M", "password": "synthetic-pass-3", "invite_token": inv}).status_code == 200
        anon = TestClient(app, raise_server_exceptions=False)

        # ---- authorization on every new route
        check("anonymous cannot list", anon.get("/api/msme/documents").status_code == 401)
        check("anonymous cannot upload", up(anon).status_code == 401)
        check("viewer cannot upload", up(viewer).status_code == 403)
        for label, call in (("process", lambda: viewer.post("/api/msme/documents/MDOC-000000000000/process")),
                            ("correct", lambda: viewer.put("/api/msme/documents/MDOC-000000000000/fields", json={"field_path": "a", "value": "b", "reason": "c"})),
                            ("approve", lambda: viewer.post("/api/msme/documents/MDOC-000000000000/approve")),
                            ("sweep", lambda: viewer.post("/api/msme/documents/sweep"))):
            check(f"viewer cannot {label}", call().status_code == 403)
        check("manager cannot sweep (admin only)", mgr.post("/api/msme/documents/sweep").status_code == 403)
        check("viewer can list", viewer.get("/api/msme/documents").status_code == 200)

        # ---- hostile uploads
        for label, kw, code, status in (
            ("html declared as pdf", dict(data=b"<html><script>1</script></html>"), "invalid_file", 400),
            ("executable declared as pdf", dict(data=b"MZ\x90\x00" + b"\0" * 64), "invalid_file", 400),
            ("csv is unsupported", dict(data=b"a,b\n1,2\n", name="a.csv", mime="text/csv"), "unsupported_type", 415),
            ("empty file", dict(data=b""), "empty_file", 400),
        ):
            r = up(mgr, **kw) if kw.get("data") != b"" else mgr.post("/api/msme/documents", files={"file": ("e.pdf", b"", "application/pdf")})
            body = r.json().get("detail", {}) if r.status_code != 500 else {}
            check(f"{label} -> {status} {code}", r.status_code == status and body.get("code") == code, f"{r.status_code} {r.text[:120]}")

        # ---- happy path: upload -> background process -> read
        data = pdf()
        r = up(mgr, data)
        check("manager upload returns 201 with a document", r.status_code == 201 and not r.json()["duplicate"], r.text[:150])
        doc_id = r.json()["document"]["id"]
        check("upload response never exposes the storage key", "storage_key" not in r.text)
        g = viewer.get(f"/api/msme/documents/{doc_id}").json()
        check("background processing completed after the response", g["status"] == "extracted" and len(g["fields"]) >= 5, str(g["status"]))
        check("fields carry evidence (page, bbox, confidence)", all(f["page"] == 1 and f["bbox"] and f["confidence"] == 0.97 for f in g["fields"]))

        r = up(mgr, data, name="again.pdf")
        check("identical upload returns 200 duplicate=true and same id", r.status_code == 200 and r.json()["duplicate"] and r.json()["document"]["id"] == doc_id, r.text[:150])
        r = up(mgr, pdf(), process="false")
        check("process=false leaves the document queued", r.json()["document"]["status"] == "uploaded")

        # ---- original download
        d = viewer.get(f"/api/msme/documents/{doc_id}/file")
        check("original is returned byte-for-byte", d.status_code == 200 and d.content == data)
        check("download headers: nosniff, no-store, inline pdf",
              d.headers.get("x-content-type-options") == "nosniff" and "no-store" in d.headers.get("cache-control", "")
              and d.headers.get("content-disposition", "").startswith("inline"), str(dict(d.headers)))

        # ---- bad ids
        check("malformed id -> 422", viewer.get("/api/msme/documents/not-an-id").status_code == 422)
        check("unknown id -> 404 with code", viewer.get("/api/msme/documents/MDOC-000000000000").json().get("detail", {}).get("code") == "not_found")

        # ---- review flow over HTTP
        current["values"] = dict(BASE, invoice_number="INV-BAD-1", total_amount="1500")
        bad = up(mgr).json()["document"]["id"]
        current["values"] = None
        g = viewer.get(f"/api/msme/documents/{bad}").json()
        check("inconsistent invoice needs review with a CONFIRMED issue",
              g["status"] == "needs_review" and any(i["severity"] == "CONFIRMED" for i in g["issues"]))
        r = mgr.post(f"/api/msme/documents/{bad}/approve")
        check("approve blocked -> 409 confirmed_issues_open", r.status_code == 409 and r.json()["detail"]["code"] == "confirmed_issues_open", r.text[:150])
        r = viewer.post(f"/api/msme/documents/{bad}/issues/acknowledge", json={"code": "TOTAL_MISMATCH", "reason": "x"})
        check("viewer cannot acknowledge issues", r.status_code == 403)
        r = mgr.post(f"/api/msme/documents/{bad}/issues/acknowledge", json={"code": "TOTAL_MISMATCH", "reason": ""})
        check("acknowledge without a reason -> 422", r.status_code == 422)
        r = mgr.post(f"/api/msme/documents/{bad}/issues/acknowledge", json={"code": "NOPE", "reason": "x"})
        check("acknowledge unknown issue -> 404 issue_not_found", r.status_code == 404 and r.json()["detail"]["code"] == "issue_not_found", r.text[:120])
        r = mgr.put(f"/api/msme/documents/{bad}/fields", json={"field_path": "total_amount", "value": "1180", "reason": ""})
        check("empty reason rejected by validation (422)", r.status_code == 422)
        r = mgr.put(f"/api/msme/documents/{bad}/fields", json={"field_path": "total_amount", "value": "1180", "reason": "misread 1180 as 1500"})
        check("correction accepted", r.status_code == 200 and not any(i["severity"] == "CONFIRMED" for i in r.json()["issues"]), r.text[:200])
        h = viewer.get(f"/api/msme/documents/{bad}/history").json()["corrections"]
        check("history visible to viewers with the acting user", len(h) == 1 and h[0]["corrected_by"] and h[0]["old_value"] == "1500")
        r = mgr.post(f"/api/msme/documents/{bad}/approve")
        check("approve succeeds after the fix", r.status_code == 200 and r.json()["status"] == "approved" and r.json()["approved_by"])

        # ---- listing, sweep, admin
        lst = viewer.get("/api/msme/documents", params={"status": "approved", "limit": 5}).json()
        check("list filters by status", lst["total"] >= 1 and all(x["status"] == "approved" for x in lst["rows"]))
        check("list rejects absurd limits", viewer.get("/api/msme/documents", params={"limit": 100000}).status_code == 422)
        r = admin.post("/api/msme/documents/sweep")
        check("admin sweep processes the queued document", r.status_code == 200 and r.json()["processed"] >= 1, r.text)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
