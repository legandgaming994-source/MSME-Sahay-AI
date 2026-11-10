#!/usr/bin/env python3
"""S-4 regression: black-box SQL-injection probes over every GET operation.

Every string-typed query parameter of every GET route (from the OpenAPI schema) is sent a set of
injection payloads as an authenticated viewer. Fails on: any 5xx (a SQL error is the classic
injection signal), SQL-engine error text in a response, schema leakage via UNION (canary table
`_SchemaMigrations`), a tautology widening a no-match search, or damage to canary tables.

Complements (does not replace) review of the 45 bandit B608 findings; see docs/msme/GAP_REGISTER.md.

Run:  python -m tests.test_sql_injection_probes
"""

import os
import re
import sys
import tempfile
import warnings

PAYLOADS = [
    "' OR '1'='1", "' OR 1=1--", "\" OR \"1\"=\"1", "x'; DROP TABLE \"Customer\";--",
    "1; DELETE FROM \"Customer\"", "' UNION SELECT name FROM sqlite_master--",
    "' UNION SELECT name,name,name FROM sqlite_master--", "name; SELECT 1", "name) OR (1=1",
    "1 OR 1=1", "%' OR '%'='", "\\", "'", "\"", "\x00", "name DESC, (SELECT 1)",
    "(CASE WHEN 1=1 THEN name ELSE id END)", "name--", "/*", "' || (SELECT 1) || '",
]
ERROR_MARKERS = re.compile(r"sqlite3\.|OperationalError|psycopg|syntax error|SQLSTATE|unrecognized token|"
                           r"near \"|no such (column|table)|unterminated|UndefinedColumn", re.I)
CANARY = "_SchemaMigrations"
PATH_VALUES = {"doctype_slug": ["sales-invoice", "quotation", "payment-entry"],
               "master_type": ["customer", "supplier", "item", "account"]}


def main() -> int:
    warnings.filterwarnings("ignore")
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="lambda_sqli_")
    os.close(fd)
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-sqli", "OPENAI_API_KEY": "synthetic-not-used"})
    from fastapi.testclient import TestClient
    from api.main import app
    from lambda_erp.database import get_db

    failures: list[str] = []

    if os.environ.get("SQLI_POSITIVE_CONTROL") == "1":
        # Positive control: a deliberately injectable route. The detector MUST flag it (exit 1).
        def _vulnerable(q: str = ""):
            return {"rows": get_db().sql(f"SELECT name FROM \"Customer\" WHERE name = '{q}'")}

        app.add_api_route("/api/_vulnerable_control", _vulnerable, methods=["GET"])
        app.router.routes.insert(0, app.router.routes.pop())  # ahead of the SPA catch-all

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    with TestClient(app, raise_server_exceptions=False) as admin:
        assert admin.post("/api/auth/register", json={"email": "a@x.invalid", "full_name": "A",
                                                      "password": "synthetic-pass-1"}).status_code == 200
        admin.put("/api/auth/settings", json={"allow_public_signup": "1"})
        viewer = TestClient(app, raise_server_exceptions=False)
        assert viewer.post("/api/auth/register", json={"email": "v@x.invalid", "full_name": "V",
                                                       "password": "synthetic-pass-2"}).status_code == 200
        d = get_db()
        for i in range(5):
            d.insert("Customer", {"name": f"CUST-{i}", "customer_name": f"Customer {i}"})
        d.commit()
        customers_before = d.sql('SELECT COUNT(*) AS n FROM "Customer"')[0]["n"]

        spec = app.openapi()
        ops = []
        for path, item in spec["paths"].items():
            if not path.startswith("/api") or "get" not in item:
                continue
            params = item["get"].get("parameters", [])
            q = [p["name"] for p in params if p["in"] == "query"
                 and p.get("schema", {}).get("type", "string") in ("string",) or
                 (p["in"] == "query" and "anyOf" in p.get("schema", {}))]
            ops.append((path, q))
        probes = errors = 0
        offenders: list[str] = []
        for path, qnames in ops:
            if not qnames:
                continue
            variants = [{}]
            for pname, values in PATH_VALUES.items():
                if "{" + pname + "}" in path:
                    variants = [{pname: v} for v in values]
            for variant in variants:
                url = path
                for pname, v in variant.items():
                    url = url.replace("{" + pname + "}", v)
                url = re.sub(r"\{[^}]+\}", "x", url)
                for qn in qnames:
                    for payload in PAYLOADS:
                        r = viewer.get(url, params={qn: payload})
                        probes += 1
                        text = r.text
                        if r.status_code >= 500 or ERROR_MARKERS.search(text) or CANARY in text:
                            errors += 1
                            if len(offenders) < 12:
                                offenders.append(f"{path} ?{qn}={payload!r} -> {r.status_code} {text[:90]!r}")
        print(f"INFO  {probes} probes over {len([o for o in ops if o[1]])} GET operations")
        check("no 5xx, SQL error text or schema canary in any probe response", errors == 0, "; ".join(offenders))

        # tautology must not widen a no-match search
        base = viewer.get("/api/masters/customer", params={"search": "zzz-no-such-customer"}).json()
        taut = viewer.get("/api/masters/customer", params={"search": "zzz' OR '1'='1"}).json()
        n_base = len(base.get("rows", [])); n_taut = len(taut.get("rows", []))
        check("tautology does not widen a no-match search (masters)", n_base == 0 and n_taut == 0, f"{n_base} vs {n_taut}")
        base = viewer.get("/api/documents/sales-invoice", params={"search": "zzz-no-such"}).json()
        taut = viewer.get("/api/documents/sales-invoice", params={"search": "zzz' OR '1'='1"}).json()
        check("tautology search handled on documents list", len(taut.get("rows", [])) == len(base.get("rows", [])),
              str((len(base.get("rows", [])), len(taut.get("rows", [])))))

        d2 = get_db()
        check("canary table intact after all probes (no DROP/DELETE executed)",
              d2.sql('SELECT COUNT(*) AS n FROM "Customer"')[0]["n"] == customers_before)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
