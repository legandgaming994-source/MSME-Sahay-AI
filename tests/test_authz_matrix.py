#!/usr/bin/env python3
"""S-1 regression: authorization matrix and cross-user object isolation.

Part A  Route inventory (from the OpenAPI schema): anonymous callers may reach only an
        explicit allowlist; viewers may reach only an explicit allowlist of write routes;
        nothing may answer 5xx to those probes. A NEW route that is anonymously reachable or
        viewer-writable fails this test until it is reviewed and added to the allowlists.
Part B  Real-ID cross-user probes: chat sessions, attachments, API keys.
Part C  Vertical privilege: role escalation, admin-only routes, live demotion/disable.
Part D  Cookie flags.
Part E  Characterisation (documents current design, not a security claim): business data is
        shared by every authenticated user of a deployment, so the DEPLOYMENT is the tenant
        boundary. Row-level company scoping does not exist. See docs/msme/TENANCY.md.

Run:  python -m tests.test_authz_matrix
"""

import os
import re
import sys
import tempfile
import warnings

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"

# Reviewed 2026-10-17. Operations an unauthenticated caller may reach (status not in 401/403/404/405).
ANON_ALLOWED = {
    ("GET", "/api/auth/oauth/providers"), ("GET", "/api/auth/public-manager"),
    ("GET", "/api/auth/setup-status"), ("GET", "/api/chat-doctypes"), ("GET", "/api/health"),
    ("GET", "/api/setup/status"), ("GET", "/api/ready"),   # /api/ready: minimal platform health check, no details
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/register"),
    # RFC 7591 dynamic client registration, required by MCP clients. Bounded: 16 KB body, redirect-URI
    # validation, S256 PKCE enforced at authorize, feature-flagged, 1000 registrations/hour deployment-wide.
    # Residual risk: anonymous callers can exhaust the hourly cap (availability); rate-limit at the edge.
    ("POST", "/api/mcp-oauth/register"),
}

# Reviewed 2026-10-17. Write operations a plain viewer may reach: all are self-service or
# ownership-scoped (own chat, own keys, own settings, own drafts, PDF render of readable docs).
VIEWER_WRITE_ALLOWED = {
    ("DELETE", "/api/auth/api-keys/{key_id}"), ("DELETE", "/api/chat/sessions/{session_id}"),
    ("DELETE", "/api/reports/runtime/drafts/{report_id}"), ("DELETE", "/api/v1/chat/sessions/{session_id}"),
    ("POST", "/api/actions/{action_name}"), ("POST", "/api/auth/api-keys"),
    ("POST", "/api/auth/api-keys/{key_id}/revoke"), ("POST", "/api/auth/change-password"),
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"), ("POST", "/api/auth/register"),
    ("POST", "/api/auth/set-password"), ("POST", "/api/auth/{provider}/callback"),
    ("POST", "/api/chat/attachments"), ("POST", "/api/chat/sessions"),
    ("POST", "/api/documents/{doctype_slug}/{name}/pdf"), ("POST", "/api/mcp-oauth/consent"),
    ("POST", "/api/reports/runtime/data"), ("POST", "/api/reports/runtime/drafts"),
    ("POST", "/api/v1/chat"), ("POST", "/api/v1/documents/{doctype_slug}/{name}/pdf"),
    ("POST", "/api/mcp-oauth/register"),
    ("PUT", "/api/auth/my-settings"), ("PUT", "/api/chat/sessions/{session_id}/title"),
    ("PUT", "/api/reports/runtime/drafts/{report_id}"),
}
SKIP_PROBE = {("POST", "/api/auth/logout")}  # would end the probing session


def main() -> int:
    warnings.filterwarnings("ignore")
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="lambda_authz_")
    os.close(fd)
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-authz-matrix", "OPENAI_API_KEY": "synthetic-not-used"})
    from fastapi.testclient import TestClient
    from api.main import app
    from lambda_erp.database import get_db

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    def client() -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    with TestClient(app, raise_server_exceptions=False) as admin:
        r = admin.post("/api/auth/register", json={"email": "admin@x.invalid", "full_name": "Admin",
                                                    "password": "synthetic-admin-pass"})
        assert r.status_code == 200 and r.json()["role"] == "admin", r.text
        admin_id = r.json()["name"]
        assert admin.put("/api/auth/settings", json={"allow_public_signup": "1", "rest_api_enabled": "1"}).status_code == 200

        def signup(email: str, role: str = "viewer"):
            c = client()
            if role == "viewer":
                r = c.post("/api/auth/register", json={"email": email, "full_name": email, "password": "synthetic-user-pass"})
            else:
                inv = admin.post("/api/auth/invite", json={"email": email, "role": role})
                assert inv.status_code == 200, inv.text
                r = c.post("/api/auth/register", json={"email": email, "full_name": email,
                                                        "password": "synthetic-user-pass", "invite_token": inv.json()["token"]})
            assert r.status_code == 200 and r.json()["role"] == role, r.text
            return c, r.json()["name"]

        v1, v1_id = signup("v1@x.invalid")
        v2, v2_id = signup("v2@x.invalid")
        mgr, mgr_id = signup("mgr@x.invalid", "manager")
        anon = client()

        # ---------------- Part A: route inventory --------------------------------------
        spec = app.openapi()["paths"]
        ops = sorted((m.upper(), p) for p, d in spec.items() for m in d
                     if m.upper() not in ("HEAD", "OPTIONS") and p.startswith("/api"))
        check("inventory is non-trivial (>100 operations)", len(ops) > 100, str(len(ops)))
        anon_open, viewer_write, fivexx = [], [], []
        for method, path in ops:
            if (method, path) in SKIP_PROBE:
                continue
            url = re.sub(r"\{[^}]+\}", "x", path)
            if not v1.cookies.get("lambda_erp_token"):
                v1.post("/api/auth/login", json={"email": "v1@x.invalid", "password": "synthetic-user-pass"})
            kw = {} if method in ("GET", "DELETE") else {"json": {}}
            a = anon.request(method, url, **kw).status_code
            v = v1.request(method, url, **kw).status_code
            if a >= 500 or v >= 500:
                fivexx.append((method, path, a, v))
            if a not in (401, 403, 404, 405):
                anon_open.append((method, path))
            if method != "GET" and v not in (401, 403):
                viewer_write.append((method, path))
        check("no 5xx from anonymous/viewer probes", not fivexx, str(fivexx))
        unexpected_anon = sorted(set(anon_open) - ANON_ALLOWED)
        check("anonymous reach limited to the reviewed allowlist", not unexpected_anon, str(unexpected_anon))
        unexpected_write = sorted(set(viewer_write) - VIEWER_WRITE_ALLOWED)
        check("viewer write reach limited to the reviewed allowlist", not unexpected_write, str(unexpected_write))
        v1.post("/api/auth/login", json={"email": "v1@x.invalid", "password": "synthetic-user-pass"})

        # ---------------- Part B: cross-user object isolation --------------------------
        s1 = v1.post("/api/chat/sessions").json()["id"]
        up = v1.post("/api/chat/attachments", files={"file": ("secret.pdf", PDF, "application/pdf")}, data={"session_id": s1})
        check("owner can upload to own session", up.status_code == 200, up.text[:100])
        att = up.json().get("id")
        v1.put(f"/api/chat/sessions/{s1}/title", json={"title": "V1 private title"})

        listed = [s["id"] for s in v2.get("/api/chat/sessions").json()]
        check("other viewer cannot list the session", s1 not in listed, str(listed))
        body = v2.get(f"/api/chat/sessions/{s1}").text
        check("other viewer cannot read the session", "V1 private title" not in body and v1_id not in body, body[:120])
        v2.put(f"/api/chat/sessions/{s1}/title", json={"title": "HACKED"})
        v2.delete(f"/api/chat/sessions/{s1}")
        mine = v1.get(f"/api/chat/sessions/{s1}")
        check("other viewer cannot rename or delete the session",
              "V1 private title" in mine.text and "HACKED" not in mine.text, mine.text[:160])
        r = v2.post("/api/chat/attachments", files={"file": ("x.pdf", PDF, "application/pdf")}, data={"session_id": s1})
        check("other viewer cannot attach into a foreign session", r.status_code == 404, str(r.status_code))
        check("other viewer cannot download the attachment", v2.get(f"/api/chat/attachments/{att}").status_code in (403, 404))
        check("manager (non-owner) cannot download the attachment", mgr.get(f"/api/chat/attachments/{att}").status_code in (403, 404))
        check("anonymous cannot download the attachment", anon.get(f"/api/chat/attachments/{att}").status_code in (401, 403, 404))
        check("admin may download any attachment (documented design)", admin.get(f"/api/chat/attachments/{att}").status_code == 200)

        k = v1.post("/api/auth/api-keys", json={"name": "v1 key"})
        check("viewer can mint own key", k.status_code == 200, k.text[:100])
        key_id = k.json()["id"]
        check("other viewer cannot see foreign keys", key_id not in [x["id"] for x in v2.get("/api/auth/api-keys").json()])
        check("other viewer cannot revoke foreign key", v2.post(f"/api/auth/api-keys/{key_id}/revoke").status_code in (403, 404))
        check("other viewer cannot delete foreign key", v2.delete(f"/api/auth/api-keys/{key_id}").status_code in (403, 404))
        still = [x for x in v1.get("/api/auth/api-keys").json() if x["id"] == key_id]
        check("foreign revoke/delete attempts left the key intact", still and not still[0].get("revoked"), str(still))
        check("manager cannot see foreign keys", key_id not in [x["id"] for x in mgr.get("/api/auth/api-keys").json()])
        check("viewer cannot mint a key above own role",
              v1.post("/api/auth/api-keys", json={"name": "esc", "role": "admin"}).status_code == 403)

        # ---------------- Part C: vertical privilege -----------------------------------
        check("viewer cannot self-promote", v1.put(f"/api/auth/users/{v1_id}/role", json={"role": "admin"}).status_code == 403)
        check("viewer cannot invite", v1.post("/api/auth/invite", json={"email": "z@x.invalid", "role": "admin"}).status_code == 403)
        check("viewer cannot change settings", v1.put("/api/auth/settings", json={"allow_public_signup": "1"}).status_code == 403)
        check("viewer cannot create documents", v1.post("/api/documents/sales-invoice", json={}).status_code == 403)
        for label, call in (("manager cannot list users", lambda: mgr.get("/api/auth/users")),
                            ("manager cannot invite", lambda: mgr.post("/api/auth/invite", json={"email": "q@x.invalid", "role": "viewer"})),
                            ("manager cannot change roles", lambda: mgr.put(f"/api/auth/users/{v1_id}/role", json={"role": "manager"})),
                            ("manager cannot change settings", lambda: mgr.put("/api/auth/settings", json={"x": "1"})),
                            ("manager cannot disable users", lambda: mgr.delete(f"/api/auth/users/{v1_id}"))):
            check(label, call().status_code == 403)
        r = mgr.post("/api/documents/sales-invoice", json={})
        check("manager passes the role gate for documents (validation error, not 403)", r.status_code != 403, str(r.status_code))

        check("demotion takes effect on the live session",
              admin.put(f"/api/auth/users/{mgr_id}/role", json={"role": "viewer"}).status_code == 200
              and mgr.post("/api/documents/sales-invoice", json={}).status_code == 403)
        check("disabling a user invalidates the live session",
              admin.delete(f"/api/auth/users/{v2_id}").status_code == 200 and v2.get("/api/auth/me").status_code == 401)
        check("admin cannot disable self / demote sole admin",
              admin.delete(f"/api/auth/users/{admin_id}").status_code == 409
              and admin.put(f"/api/auth/users/{admin_id}/role", json={"role": "viewer"}).status_code == 409)

        # ---------------- Part D: cookie flags -----------------------------------------
        r = client().post("/api/auth/login", json={"email": "v1@x.invalid", "password": "synthetic-user-pass"})
        cookie = r.headers.get("set-cookie", "").lower()
        check("session cookie is HttpOnly + SameSite", "httponly" in cookie and "samesite=lax" in cookie, cookie)
        r = client().post("/api/auth/login", json={"email": "v1@x.invalid", "password": "synthetic-user-pass"},
                          headers={"X-Forwarded-Proto": "https"})
        check("session cookie is Secure behind an HTTPS proxy", "secure" in r.headers.get("set-cookie", "").lower(),
              r.headers.get("set-cookie", ""))
        check("wrong password rejected", client().post("/api/auth/login",
              json={"email": "v1@x.invalid", "password": "nope"}).status_code in (400, 401, 403))

        # ---------------- Part E: characterisation of the tenant boundary --------------
        d = get_db()
        for co in ("Tenant A Co", "Tenant B Co"):
            d.insert("Company", {"name": co, "company_name": co, "default_currency": "INR", "tax_id": f"TAX-{co[7]}"})
        d.insert("Account", {"name": "Rev - B", "account_name": "Rev", "company": "Tenant B Co",
                             "root_type": "Income", "report_type": "Profit and Loss", "is_group": 0})
        d.commit()
        companies = {row["name"]: row for row in v1.get("/api/masters/company").json()["rows"]}
        accounts = [row["company"] for row in v1.get("/api/masters/account").json()["rows"]]
        check("CHARACTERISATION: a plain viewer reads every company's master data incl. tax_id "
              "(no row-level tenancy; deployment is the tenant boundary)",
              {"Tenant A Co", "Tenant B Co"} <= set(companies) and companies["Tenant B Co"].get("tax_id") == "TAX-B"
              and "Tenant B Co" in accounts, str(sorted(companies)))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
