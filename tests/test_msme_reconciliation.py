#!/usr/bin/env python3
"""F-1 / R-5: document <-> bank reconciliation and the turnover cross-check on a real database.

Real approved documents (Pass 4 service), real Bank Account / Bank Transaction rows, real compliance profile.
Guarantees checked: bank data is never modified, the DATABASE rejects a second confirmed match, concurrent
confirmations produce exactly one winner, rejected pairs are never re-suggested, runs are reproducible,
and every change is in the hash-chained audit trail.

Run:  python -m tests.test_msme_reconciliation      (LAMBDA_ERP_TEST_DB=postgresql://... for PostgreSQL)
"""

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

sys.dont_write_bytecode = True

CO_GSTIN, OTHER = "27AAPFU0939F1ZV", "09AAACT2727Q1ZU"


def main() -> int:
    from tests._msme_db import assert_backend, reset_test_db

    db_path = reset_test_db("lambda_msme_recon_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_recon_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-msme-recon"})
    from lambda_erp.database import get_db, setup
    setup(db_path)
    assert_backend(bool(os.environ.get("LAMBDA_ERP_TEST_DB")))

    from msme.compliance.service import ComplianceService
    from msme.credit.service import CreditService
    from msme.documents.extraction import ExtractedField, ExtractionOutcome
    from msme.documents.service import DocumentService
    from msme.documents.storage import LocalDocumentStorage
    from msme.reconciliation.service import ReconciliationService, ReconError

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    def raises(code, fn, *a, **k):
        try:
            fn(*a, **k)
        except ReconError as exc:
            return exc.code == code
        return False

    NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    clock = lambda: NOW  # noqa: E731
    rec, comp, credit = ReconciliationService(clock=clock), ComplianceService(clock=clock), CreditService(clock=clock)
    db = get_db()
    for name in ("Acme Traders", "Other Co", "No Profile Co", "No Gstin Co"):
        db.insert("Company", {"name": name, "company_name": name, "default_currency": "INR"})
    for co, acct in (("Acme Traders", "Bank - A"), ("Other Co", "Bank - O")):
        db.insert("Account", {"name": acct, "account_name": acct, "company": co, "root_type": "Asset", "report_type": "Balance Sheet", "is_group": 0})
    db.insert("Bank Account", {"name": "BA-A", "account_name": "A", "company": "Acme Traders", "account": "Bank - A", "iban": "IN00SYNTH0000000001", "currency": "INR"})
    db.insert("Bank Account", {"name": "BA-O", "account_name": "O", "company": "Other Co", "account": "Bank - O", "iban": "IN00SYNTH0000000002", "currency": "INR"})
    db.commit()
    comp.set_profile("Acme Traders", "regular_monthly", None, CO_GSTIN, "alice")
    comp.set_profile("No Gstin Co", "regular_monthly", None, None, "alice")

    def txn(name, date, amount, kind, desc, party, status="Unreconciled", reversal=0, currency="INR", acct="BA-A", bank="Bank - A"):
        db.insert("Bank Transaction", {"name": name, "bank_account": bank, "bank_account_id": acct, "posting_date": date,
                                       "deposit": amount if kind == "deposit" else 0, "withdrawal": amount if kind == "withdrawal" else 0,
                                       "currency": currency, "description": desc, "counterparty_name": party, "status": status,
                                       "reversal_indicator": reversal, "docstatus": 1})
        db.commit()

    txn("T1", "2026-06-25", 1180, "deposit", "NEFT KURNOOL TRADERS INV20260042", "KURNOOL TRADERS")
    txn("T2", "2026-07-05", 2124, "deposit", "NEFT BETA FOODS INV20260050 LESS TDS", "BETA FOODS")
    txn("T3", "2026-06-20", 590, "withdrawal", "IMPS DELTA SUPPLIES SUP7781X", "DELTA SUPPLIES")
    txn("T4", "2026-06-30", 3000, "deposit", "NEFT 88231", "ZETA CORP")
    txn("T5", "2026-06-18", 777, "deposit", "UPI MISC CREDIT", "UNKNOWN")
    txn("T6", "2026-06-25", 1180, "deposit", "NEFT KURNOOL TRADERS INV20260042", "KURNOOL TRADERS", status="Informational")
    txn("T7", "2026-06-25", 1180, "deposit", "NEFT KURNOOL TRADERS INV20260042", "KURNOOL TRADERS", reversal=1)
    txn("T8", "2026-06-25", 1180, "deposit", "NEFT KURNOOL TRADERS INV20260042", "KURNOOL TRADERS", acct="BA-O", bank="Bank - O")
    txn("T9", "2026-06-25", 1180, "deposit", "WIRE KURNOOL TRADERS INV20260042", "KURNOOL TRADERS", currency="USD")
    bank_before = [dict(r) for r in db.sql('SELECT * FROM "Bank Transaction" ORDER BY name')]

    # ---------------- approved documents ----------------
    n, cur = [0], {"values": None, "type": "invoice"}

    class Fake:
        name, version = "fake", "1"

        def extract(self, path):
            fields = [ExtractedField(p, v, 0.97, 1, v, {"x0": .1, "y0": .1, "x1": .2, "y1": .2}, "verified") for p, v in cur["values"].items()]
            return ExtractionOutcome("extracted", cur["type"], fields, [], extractor="fake", extractor_version="1")

    docs = DocumentService(Fake(), LocalDocumentStorage(storage_dir))

    def invoice(number, issued, subtotal, tax, party, direction="outward", due=None, doc_type="invoice", currency="INR", approve=True, gst=True):
        total = f"{float(subtotal) + float(tax):.2f}"
        seller, buyer = (CO_GSTIN, OTHER) if direction == "outward" else (OTHER, CO_GSTIN)
        v = {"invoice_number": number, "issue_date": issued, "subtotal": subtotal, "tax_amount": tax, "total_amount": total, "currency": currency,
             "seller.name": "Acme Traders" if direction == "outward" else party, "buyer.name": party if direction == "outward" else "Acme Traders"}
        if gst:
            v.update({"seller.tax_ids[0].value": seller, "seller.tax_ids[0].scheme": "gst", "buyer.tax_ids[0].value": buyer, "buyer.tax_ids[0].scheme": "gst"})
        if due:
            v["due_date"] = due
        cur["values"], cur["type"] = v, doc_type
        n[0] += 1
        d, _ = docs.ingest("u", f"r{n[0]}.pdf", f"%PDF-1.4\n% recon {n[0]}\n%%EOF\n".encode(), "application/pdf")
        docs.process(d["id"])
        if approve:
            docs.approve(d["id"], "reviewer")
        return d["id"]

    out_a = invoice("INV-2026-0042", "2026-06-10", "1000.00", "180.00", "Kurnool Traders Pvt Ltd", due="2026-07-10")
    out_b = invoice("INV-2026-0050", "2026-06-12", "2000.00", "360.00", "Beta Foods Ltd", due="2026-07-12")
    out_c = invoice("INV-2026-0060", "2026-06-15", "400.00", "100.00", "Gamma Stores")
    in_a = invoice("SUP-7781-X", "2026-06-05", "500.00", "90.00", "Delta Supplies", direction="inward")
    amb1 = invoice("AMB-1001", "2026-06-20", "3000.00", "0.00", "Zeta Corp")
    amb2 = invoice("AMB-2002", "2026-06-21", "3000.00", "0.00", "Zeta Corp")
    dup = invoice("INV-2026-0042", "2026-06-10", "1000.00", "180.00", "Kurnool Traders Pvt Ltd", due="2026-07-10")
    credit_note = invoice("CN-1", "2026-06-12", "50.00", "9.00", "Kurnool Traders", doc_type="credit_note")
    usd = invoice("USD-1", "2026-06-13", "100.00", "0.00", "Kurnool Traders", currency="USD")
    unclassified = invoice("UN-1", "2026-06-14", "100.00", "18.00", "Kurnool Traders", gst=False)
    check("setup: the duplicate invoice is flagged as a duplicate of the first", docs.get_document(dup)["duplicate_of"] == out_a)

    # ---------------- guards ----------------
    check("no compliance profile -> profile_required", raises("profile_required", rec.run, "No Profile Co", "alice"))
    check("a profile without a GSTIN -> gstin_required", raises("gstin_required", rec.run, "No Gstin Co", "alice"))

    # ---------------- run: suggestions, explanation, exclusions ----------------
    r = rec.run("Acme Traders", "alice")
    pairs = {(m["document"]["id"], m["bank_transaction"]["name"]): m for m in rec.list_matches("Acme Traders", limit=200)["rows"]}
    primary = {k: m for k, m in pairs.items() if m["status"] == "suggested"}
    check("exact amount + invoice number + name + timing is STRONG with named reasons",
          primary[(out_a, "T1")]["classification"] == "STRONG" and {x["code"] for x in primary[(out_a, "T1")]["reasons"]} == {"AMOUNT_EXACT", "REFERENCE", "NAME", "TIMING"}, str(primary.get((out_a, "T1"))))
    check("a 10% shortfall is PROBABLE and flagged as a possible deduction (no rate assumed)",
          primary[(out_b, "T2")]["classification"] == "PROBABLE" and primary[(out_b, "T2")]["difference"] == "-236.00"
          and "no rate is assumed" in " ".join(x["detail"] for x in primary[(out_b, "T2")]["reasons"]), str(primary.get((out_b, "T2"))))
    check("a purchase invoice matches a withdrawal", primary[(in_a, "T3")]["classification"] == "STRONG")
    amb = [m for m in pairs.values() if m["document"]["id"] in (amb1, amb2) and m["bank_transaction"]["name"] == "T4"]
    check("two identical invoices competing for one payment: one primary, one alternative, both flagged ambiguous",
          sorted(m["status"] for m in amb) == ["alternative", "suggested"] and all(m["ambiguous"] for m in amb), str(amb))
    seen_txns = {m["bank_transaction"]["name"] for m in pairs.values()}
    check("informational, reversed, other-company and foreign-currency transactions are never suggested", not (seen_txns & {"T6", "T7", "T8", "T9"}), str(seen_txns))
    check("duplicate, credit note, non-INR and unclassified documents are listed, not matched",
          r["document_attention"] == {"duplicates": [dup], "credit_notes": [credit_note], "non_inr": [usd], "unclassified": [unclassified]}, str(r["document_attention"]))
    check("foreign-currency transactions are reported", r["transaction_attention"] == {"non_inr": ["T9"]}, str(r["transaction_attention"]))
    sig = lambda: sorted((m["document"]["id"], m["bank_transaction"]["name"], m["status"], m["score"]) for m in rec.list_matches("Acme Traders", limit=200)["rows"])  # noqa: E731
    first = sig()
    rec.run("Acme Traders", "bob")
    check("running again is reproducible (same pairs, statuses and scores)", sig() == first)

    # ---------------- confirm / reject ----------------
    m_a = primary[(out_a, "T1")]
    done = rec.confirm(m_a["id"], "bob", "Checked against the statement")
    check("confirming records who, when and why", done["status"] == "confirmed" and done["decided_by"] == "bob" and done["note"].startswith("Checked"))
    check("confirming the match leaves no competing live match for that invoice or payment",
          not [m for m in rec.list_matches("Acme Traders", limit=200)["rows"] if m["status"] in ("suggested", "alternative", "superseded")
               and (m["document"]["id"] == out_a or m["bank_transaction"]["name"] == "T1")])
    check("a confirmed match cannot be confirmed again", raises("not_confirmable", rec.confirm, m_a["id"], "bob"))
    check("rejection needs a reason", raises("reason_required", rec.reject, primary[(out_b, "T2")]["id"], "bob", " "))
    rej = rec.reject(primary[(out_b, "T2")]["id"], "bob", "Payment was for a different order")
    check("rejection recorded", rej["status"] == "rejected" and "different order" in rej["note"])
    rec.run("Acme Traders", "bob")
    after = {(m["document"]["id"], m["bank_transaction"]["name"]): m["status"] for m in rec.list_matches("Acme Traders", limit=200)["rows"]}
    check("a rejected pair is remembered and never re-suggested", after[(out_b, "T2")] == "rejected" and list(after.values()).count("rejected") == 1)
    check("confirmed invoices and payments leave the matching pool", after[(out_a, "T1")] == "confirmed" and not [k for k, v in after.items() if v in ("suggested", "alternative") and (k[0] == out_a or k[1] == "T1")])

    # ---------------- manual match and its guards ----------------
    check("manual: an invoice that is not eligible is refused", raises("document_not_eligible", rec.manual_match, "Acme Traders", credit_note, "T5", "alice", "x"))
    check("manual: a duplicate invoice is refused", raises("document_not_eligible", rec.manual_match, "Acme Traders", dup, "T5", "alice", "x"))
    check("manual: an informational transaction is refused", raises("transaction_not_eligible", rec.manual_match, "Acme Traders", out_b, "T6", "alice", "x"))
    check("manual: direction must match the money flow", raises("direction_mismatch", rec.manual_match, "Acme Traders", out_b, "T3", "alice", "x"))
    check("manual: a note is required", raises("reason_required", rec.manual_match, "Acme Traders", out_b, "T2", "alice", ""))
    check("manual: an already-confirmed transaction is refused", raises("already_matched", rec.manual_match, "Acme Traders", out_c, "T1", "alice", "x"))
    man = rec.manual_match("Acme Traders", out_b, "T2", "alice", "Customer confirmed this payment by phone; 236 is TDS")
    check("a person can override their own earlier rejection with a manual match",
          man["status"] == "confirmed" and man["classification"] == "MANUAL" and man["manual"] and man["difference"] == "-236.00" and man["score"] is None)
    check("manual: the invoice is now taken", raises("already_matched", rec.manual_match, "Acme Traders", out_b, "T5", "alice", "x"))

    # ---------------- the database itself refuses a second confirmed match ----------------
    try:
        db.insert("MSME Reconciliation Match", {"id": "MRCM-000000000001", "company": "Acme Traders", "document_id": out_a, "bank_transaction": "T5",
                                                "status": "confirmed", "classification": "MANUAL", "basis_json": "{}", "algorithm_version": "x",
                                                "created_by": "attacker", "created_at": "2026-01-01T00:00:00+00:00"})
        db.commit()
        refused = False
    except Exception as exc:  # noqa: BLE001
        db.conn.rollback()
        refused = "unique" in str(exc).lower() or "duplicate" in str(exc).lower()
    check("DB constraint: a second confirmed match for the same invoice is refused even by direct SQL", refused)
    try:
        db.insert("MSME Reconciliation Match", {"id": "MRCM-000000000002", "company": "Acme Traders", "document_id": out_c, "bank_transaction": "T1",
                                                "status": "confirmed", "classification": "MANUAL", "basis_json": "{}", "algorithm_version": "x",
                                                "created_by": "attacker", "created_at": "2026-01-01T00:00:00+00:00"})
        db.commit()
        refused = False
    except Exception as exc:  # noqa: BLE001
        db.conn.rollback()
        refused = "unique" in str(exc).lower() or "duplicate" in str(exc).lower()
    check("DB constraint: a second confirmed match for the same payment is refused even by direct SQL", refused)

    # ---------------- unmatch ----------------
    check("the manual match reused the rejected pair's row: ids are stable across the pair's whole lifecycle", man["id"] == rej["id"])
    check("unmatch needs a confirmed match and a reason",
          raises("not_confirmed", rec.unmatch, primary[(in_a, "T3")]["id"], "bob", "x")        # still only a suggestion
          and raises("reason_required", rec.unmatch, m_a["id"], "bob", ""))
    un = rec.unmatch(m_a["id"], "bob", "Wrong customer")
    check("unmatch reverses the confirmation, keeps the reason, and the pair is never re-suggested",
          un["status"] == "rejected" and "Wrong customer" in un["note"])
    rec.run("Acme Traders", "bob")
    check("after unmatching, the invoice and payment return to the pool but not as the rejected pair",
          not [m for m in rec.list_matches("Acme Traders", limit=200)["rows"] if (m["document"]["id"], m["bank_transaction"]["name"]) == (out_a, "T1") and m["status"] in ("suggested", "alternative")])

    # ---------------- concurrent confirmation: exactly one winner ----------------
    live = [m for m in rec.list_matches("Acme Traders", limit=200)["rows"] if m["bank_transaction"]["name"] == "T4" and m["status"] in ("suggested", "alternative")]
    check("setup: both competing invoices have a live match on the same payment", len(live) == 2, str(live))
    results = []

    def confirm(mid, user):
        try:
            rec.confirm(mid, user)
            results.append("ok")
        except ReconError as exc:
            results.append(exc.code)

    ts = [threading.Thread(target=confirm, args=(m["id"], u)) for m, u in zip(live, ("bob", "carol"))]
    [t.start() for t in ts]; [t.join() for t in ts]
    check("two people confirming competing matches: exactly one succeeds, the other is told it is taken",
          results.count("ok") == 1 and ("already_matched" in results or "not_confirmable" in results), str(results))
    check("exactly one confirmed match exists for the payment", db.sql('SELECT COUNT(*) AS n FROM "MSME Reconciliation Match" WHERE bank_transaction = ? AND status = \'confirmed\'', ["T4"])[0]["n"] == 1)

    # ---------------- defence in depth: a corrupted live row cannot be confirmed into a 503 ----------------
    db.insert("MSME Reconciliation Match", {"id": "MRCM-0000000000aa", "company": "Acme Traders", "document_id": out_c, "bank_transaction": "T4",
                                            "status": "suggested", "classification": "POSSIBLE", "score": 40, "basis_json": json.dumps({"reasons": []}),
                                            "algorithm_version": "test", "created_by": "test", "created_at": "2026-10-20T00:00:00+00:00"})
    db.commit()
    check("confirming a (corrupted) live match on an already-matched payment gives a clear 409, not a retry storm / 503",
          raises("already_matched", rec.confirm, "MRCM-0000000000aa", "bob"))
    db.sql('DELETE FROM "MSME Reconciliation Match" WHERE id = ?', ["MRCM-0000000000aa"]); db.commit()

    # ---------------- queues and summary ----------------
    ud = rec.unmatched("Acme Traders", "documents")
    ids = {r["id"] for r in ud["rows"]}
    check("unmatched invoices: the unpaid invoice and the not-yet-matched ones, with age in days", out_c in ids and out_a in ids and out_b not in ids
          and [r for r in ud["rows"] if r["id"] == out_c][0]["age_days"] == 127, str(ud["rows"]))
    ut = rec.unmatched("Acme Traders", "transactions")
    check("unmatched transactions include the unexplained credit", "T5" in {r["name"] for r in ut["rows"]} and "T6" not in {r["name"] for r in ut["rows"]})
    check("queues paginate and validate the kind", len(rec.unmatched("Acme Traders", "documents", limit=1)["rows"]) == 1 and raises("invalid_kind", rec.unmatched, "Acme Traders", "bogus"))
    s = rec.summary("Acme Traders")
    check("summary totals: matched outward value is the confirmed manual match + the confirmed competing invoice",
          s["invoices"]["outward"]["matched"] == 2 and s["invoices"]["outward"]["matched_value"] == "5360.00" and s["bank"]["deposits"]["matched"] == 2, str(s["invoices"]["outward"]))
    check("summary reports what is unmatched on both sides", s["invoices"]["inward"]["unmatched"] == 1 and s["bank"]["withdrawals"]["unmatched"] == 1)

    # ---------------- bank data is never modified ----------------
    bank_after = [dict(r) for r in db.sql('SELECT * FROM "Bank Transaction" ORDER BY name')]
    check("reconciliation never modified a single Bank Transaction row (full before/after snapshot)", bank_before == bank_after)

    # ---------------- audit trail ----------------
    v = comp.verify_audit()
    actions = {e["action"] for e in comp.audit_events(limit=500)}
    check("every reconciliation change is in a valid hash-chained audit trail",
          v["ok"] and {"recon.run", "recon.confirm", "recon.reject", "recon.unmatch", "recon.manual"} <= actions, f"{v} {actions}")

    # ---------------- turnover cross-check ----------------
    credit.set_fact("Acme Traders", "turnover_inr", 10_000_000, "alice")
    credit.set_fact("No Gstin Co", "turnover_inr", 5_000, "alice")
    t = credit.turnover_check("Other Co")
    check("no declaration -> NO_DECLARATION with guidance", t["status"] == "NO_DECLARATION" and "turnover_inr" in t["message"])
    t = credit.turnover_check("Acme Traders")
    check("evidence totals: documented outward taxable 9400.00 (excludes duplicate, credit note, USD, unclassified); bank credits 7081.00 (excludes informational, reversed, other company, USD)",
          t["documented_outward_taxable"] == "9400.00" and t["bank_deposits"] == "7081.00", str((t["documented_outward_taxable"], t["bank_deposits"])))
    codes = {f["code"] for f in t["flags"]}
    check("declared 1 crore against thin evidence -> low-coverage and low-bank-credit flags, all HEURISTIC",
          codes == {"LOW_DOCUMENT_COVERAGE", "LOW_BANK_CREDITS"} and all(f["severity"] == "HEURISTIC" for f in t["flags"]), str(t["flags"]))
    credit.set_fact("Acme Traders", "turnover_inr", 5_000, "alice")
    exceeds = credit.turnover_check("Acme Traders")["flags"]
    check("declared below the documented sales -> DOCUMENTED_EXCEEDS_DECLARED, severity HEURISTIC, mentions a different period",
          [f["code"] for f in exceeds] == ["DOCUMENTED_EXCEEDS_DECLARED"] and exceeds[0]["severity"] == "HEURISTIC"
          and "different period" in exceeds[0]["message"], str(exceeds))
    credit.set_fact("Acme Traders", "turnover_inr", 9_400, "alice")
    t = credit.turnover_check("Acme Traders")
    check("declared equal to the documented sales -> no flags", t["flags"] == [] and t["documented_ratio"] == 1.0, str(t["flags"]))
    check("caveats are always returned", len(t["caveats"]) == 3 and "financial year" in t["caveats"][0])
    check("without a company GSTIN the check says so instead of guessing", {f["code"] for f in credit.turnover_check("No Gstin Co")["flags"]} >= {"NO_GSTIN"})

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
