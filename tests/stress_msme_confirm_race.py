#!/usr/bin/env python3
"""Optional stress tool (not run in CI): 25 two-way confirmation races per backend; reports retries and any crash.

Used to find and then prove the fix for PostgreSQL DeadlockDetected in concurrent confirmations
(without deadlock retry 14/25 races crashed; with it 0/25). Run: python tests/stress_msme_confirm_race.py
(LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)."""
import os, sys, threading, tempfile, warnings
warnings.filterwarnings("ignore"); sys.path.insert(0, ".")
from tests._msme_db import reset_test_db, assert_backend
path = reset_test_db("stress_"); os.environ.update({"LAMBDA_ERP_DB": path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "", "JWT_SECRET_KEY": "x"})
from lambda_erp.database import get_db, setup
setup(path); assert_backend(bool(os.environ.get("LAMBDA_ERP_TEST_DB")))
from msme.compliance import audit
from msme.compliance.service import ComplianceService
from msme.documents.extraction import ExtractedField, ExtractionOutcome
from msme.documents.service import DocumentService
from msme.documents.storage import LocalDocumentStorage
from msme.reconciliation.service import ReconciliationService, ReconError
retries = [0]; real = audit.time.sleep
audit.time.sleep = lambda x: (retries.__setitem__(0, retries[0] + 1), real(x))[1]
db = get_db(); CO = "27AAPFU0939F1ZV"; OT = "09AAACT2727Q1ZU"
db.insert("Company", {"name": "S", "company_name": "S", "default_currency": "INR"})
db.insert("Account", {"name": "B", "account_name": "B", "company": "S", "root_type": "Asset", "report_type": "Balance Sheet", "is_group": 0})
db.insert("Bank Account", {"name": "BA", "account_name": "BA", "company": "S", "account": "B", "iban": "IN00STRESS0000000001", "currency": "INR"}); db.commit()
ComplianceService().set_profile("S", "regular_monthly", None, CO, "u")
cur = {}
class F:
    name = version = "f"
    def extract(self, p): return ExtractionOutcome("extracted", "invoice", [ExtractedField(k, v, .9, 1, v, None, "verified") for k, v in cur.items()])
docs = DocumentService(F(), LocalDocumentStorage(tempfile.mkdtemp()))
rec = ReconciliationService(); TRIALS = 25; bad = 0; outcomes = {}
for i in range(TRIALS):
    ids = []
    for j in range(2):
        cur.clear(); cur.update({"invoice_number": f"S-{i}-{j}", "issue_date": "2026-06-10", "subtotal": "1000.00", "tax_amount": "0.00", "total_amount": "1000.00", "currency": "INR",
            "seller.name": "S", "buyer.name": "Zed Corp", "seller.tax_ids[0].value": CO, "seller.tax_ids[0].scheme": "gst", "buyer.tax_ids[0].value": OT, "buyer.tax_ids[0].scheme": "gst"})
        d, _ = docs.ingest("u", f"s{i}{j}.pdf", f"%PDF-1.4\n% s {i} {j}\n%%EOF\n".encode(), "application/pdf"); docs.process(d["id"]); docs.approve(d["id"], "r"); ids.append(d["id"])
    db.insert("Bank Transaction", {"name": f"TX{i}", "bank_account": "B", "bank_account_id": "BA", "posting_date": "2026-06-20", "deposit": 1000, "withdrawal": 0, "currency": "INR",
        "description": f"NEFT ZED {i}", "counterparty_name": "ZED CORP", "status": "Unreconciled", "reversal_indicator": 0, "docstatus": 1}); db.commit()
    rec.run("S", "u")
    live = [m for m in rec.list_matches("S", limit=200)["rows"] if m["bank_transaction"]["name"] == f"TX{i}" and m["status"] in ("suggested", "alternative")]
    res = []
    def go(mid, user):
        try: rec.confirm(mid, user); res.append("ok")
        except ReconError as e: res.append(e.code)
        except Exception as e: res.append("CRASH:" + type(e).__name__)
    ts = [threading.Thread(target=go, args=(m["id"], u)) for m, u in zip(live, ("a", "b"))]
    [t.start() for t in ts]; [t.join() for t in ts]
    n = db.sql('SELECT COUNT(*) AS n FROM "MSME Reconciliation Match" WHERE bank_transaction = ? AND status = \'confirmed\'', [f"TX{i}"])[0]["n"]
    key = tuple(sorted(res)); outcomes[key] = outcomes.get(key, 0) + 1
    if not (res.count("ok") == 1 and n == 1 and not any(r.startswith("CRASH") or r == "busy" for r in res)): bad += 1
print(f"trials={TRIALS} bad={bad} retries={retries[0]} outcomes={outcomes} chain_ok={ComplianceService().verify_audit()['ok']}")
