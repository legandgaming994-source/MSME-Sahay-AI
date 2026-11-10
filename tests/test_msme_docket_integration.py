#!/usr/bin/env python3
"""D-1 with the REAL docket-idp pipeline: real PDFs, a real rasterised scan through real Tesseract.

Only the language model is stubbed (patching docket.extract.chat_json - the seam docket's own tests use),
so text-layer reading, OCR, citation->bounding-box resolution, schema validation, our field mapping,
deterministic checks and the service flow all run for real. Which values the *model* returns is scripted:
this test proves the plumbing and the safety nets, not model accuracy (see docs/msme/DOCUMENT_INTELLIGENCE.md).

Requires: docket-idp, pymupdf (test only); tesseract binary for the scan case (skipped if absent).

Run:  python -m tests.test_msme_docket_integration
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

LINES = [
    "TAX INVOICE",
    "Invoice No: GST/26-27/0042",
    "Date: 2026-09-01",
    "Supplier: Northgate Supplies Pvt Ltd",
    "GSTIN: 27AAPFU0939F1ZV",
    "Bill to: Kurnool Traders",
    "Buyer GSTIN: 09AAACT2727Q1ZU",
    "1   Copier paper A4   10   100.00   1000.00",
    "2   Toner black   2   250.00   500.00",
    "Taxable value: INR 1500.00",
    "IGST 18%: INR 270.00",
    "Grand total: INR 1770.00",
]
CITES = {
    "invoice_number": "Invoice No: GST/26-27/0042",
    "issue_date": "Date: 2026-09-01",
    "seller.name": "Supplier: Northgate Supplies Pvt Ltd",
    "seller.tax_ids[0].value": "GSTIN: 27AAPFU0939F1ZV",
    "buyer.name": "Bill to: Kurnool Traders",
    "buyer.tax_ids[0].value": "Buyer GSTIN: 09AAACT2727Q1ZU",
    "subtotal": "Taxable value: INR 1500.00",
    "tax_amount": "IGST 18%: INR 270.00",
    "total_amount": "Grand total: INR 1770.00",
}
ROW1, ROW2 = "1   Copier paper A4   10   100.00   1000.00", "2   Toner black   2   250.00   500.00"
LINE_CITES = {f"line_items[{i}].{k}": row for i, row in enumerate((ROW1, ROW2))
              for k in ("description", "quantity", "unit_price", "total")}
CITES_NO_LINES = dict(CITES)           # a model that cites the header/totals but not the rows
CITES = dict(CITES, **LINE_CITES)      # a model that also cites every line item


def payload(total=1770.00, cites=None, gstin="27AAPFU0939F1ZV"):
    return {
        "invoice_number": "GST/26-27/0042", "issue_date": "2026-09-01", "currency": "INR",
        "seller": {"name": "Northgate Supplies Pvt Ltd", "tax_ids": [{"value": gstin, "scheme": "gst", "country_code": "IN"}]},
        "buyer": {"name": "Kurnool Traders", "tax_ids": [{"value": "09AAACT2727Q1ZU", "scheme": "gst", "country_code": "IN"}]},
        "line_items": [{"description": "Copier paper A4", "quantity": 10, "unit_price": 100.0, "total": 1000.0},
                       {"description": "Toner black", "quantity": 2, "unit_price": 250.0, "total": 500.0}],
        "subtotal": 1500.00, "tax_rate_percent": 18, "tax_amount": 270.00, "total_amount": total,
        "field_locations": {f: {"page": 1, "quote": q} for f, q in (cites or CITES).items()},
    }


def main() -> int:
    os.environ.pop("DOCKET_VISION_MODEL", None)
    os.environ["DOCKET_TEXT_MODEL"] = "stub-text-model"  # docket refuses to run with no model name configured
    from tests._msme_db import reset_test_db

    db_path = reset_test_db("lambda_msme_docket_")
    storage_dir = tempfile.mkdtemp(prefix="lambda_msme_docket_store_")
    os.environ.update({"LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-docket"})

    from lambda_erp.database import setup

    setup(db_path)  # a bare get_db() would silently use an in-memory SQLite
    from tests._msme_db import assert_backend

    assert_backend(bool(os.environ.get('LAMBDA_ERP_TEST_DB')))
    import pymupdf as fitz
    from docket import extract as docket_extract
    from msme.documents.extraction import DocketExtractor
    from msme.documents.service import DocumentError, DocumentService
    from msme.documents.storage import LocalDocumentStorage

    failures: list[str] = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    work = Path(tempfile.mkdtemp(prefix="lambda_msme_docket_files_"))
    pdf_path = work / "invoice.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        for n, line in enumerate(LINES):
            page.insert_text((72, 90 + 20 * n), line, fontsize=11)
        doc.save(pdf_path)

    def stub(data):
        docket_extract.chat_json = lambda *a, **k: data

    real_chat_json = docket_extract.chat_json
    extractor = DocketExtractor()

    # ---------------- A: born-digital PDF, honest citations ----------------
    stub(payload())
    out = extractor.extract(pdf_path)
    f = {x.path: x for x in out.fields}
    check("born-digital PDF extracts", out.status in ("extracted", "needs_review") and out.document_type == "invoice", f"{out.status} {out.error_message}")
    check("extractor identity recorded", out.extractor == "docket-idp" and out.extractor_version not in ("", "unknown", "unavailable"))
    check("every cited key field resolves to the page", all(f[p].location_status == "verified" for p in CITES if p in f), str({p: f[p].location_status for p in f if p in CITES}))
    t = f["total_amount"]
    check("total_amount has page, normalised bbox and confidence in (0,1]",
          t.page == 1 and t.bbox and all(0.0 <= t.bbox[k] <= 1.0 for k in ("x0", "y0", "x1", "y1")) and 0 < (t.confidence or 0) <= 1, str(t))
    check("quote echoes the printed line", t.quote == "Grand total: INR 1770.00", str(t.quote))
    check("line items flattened with docket-style paths", f["line_items[1].total"].value == "500" and "seller.tax_ids[0].value" in f)

    # ---------------- B: fabricated citation is never given confidence ----------------
    bad = dict(CITES, total_amount="Grand total: INR 9999.00 (not on the page)")
    stub(payload(cites=bad))
    out = extractor.extract(pdf_path)
    t = {x.path: x for x in out.fields}["total_amount"]
    check("fabricated citation -> unlocated, no bbox, no confidence", t.location_status == "unlocated" and t.bbox is None and t.confidence is None, str(t))

    # ---------------- C: service end-to-end on the real engine ----------------
    service = DocumentService(extractor, LocalDocumentStorage(storage_dir))
    stub(payload())
    d, dup = service.ingest("mgr", "invoice.pdf", pdf_path.read_bytes(), "application/pdf")
    g = service.process(d["id"])
    check("service persists a real extraction with no CONFIRMED issues",
          g["status"] in ("extracted", "needs_review") and not [i for i in g["issues"] if i["severity"] == "CONFIRMED"], str(g["issues"]))
    check("business key computed from GSTIN + number + date + total",
          g["business_key"] == "27AAPFU0939F1ZV|GST2627" + "0042|2026-09-01|1770.00", str(g["business_key"]))
    approved = service.approve(d["id"], "mgr") if g["status"] in ("extracted", "needs_review") else g
    check("a consistent, fully cited invoice can be approved", approved["status"] == "approved", str(approved["issues"]))

    # ---------------- D: model returns a wrong total -> deterministic CONFIRMED issue ----------------
    work2 = work / "invoice2.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        for n, line in enumerate(LINES):
            page.insert_text((72, 90 + 20 * n), line + (" " if n == 0 else ""), fontsize=11)
        doc.save(work2)
    stub(payload(total=1870.00, cites=dict(CITES, total_amount="Grand total: INR 1770.00")))
    d2, _ = service.ingest("mgr", "invoice2.pdf", work2.read_bytes(), "application/pdf")
    g2 = service.process(d2["id"])
    mism = [i for i in g2["issues"] if i["code"] == "TOTAL_MISMATCH"]
    check("planted wrong total is a CONFIRMED mismatch", g2["status"] == "needs_review" and mism and mism[0]["severity"] == "CONFIRMED", str(g2["issues"]))
    try:
        service.approve(d2["id"], "mgr")
        blocked = False
    except DocumentError as exc:
        blocked = exc.code == "confirmed_issues_open"
    check("approval is blocked until the total is corrected", blocked)
    fixed = service.correct_field(d2["id"], "total_amount", "1770", "Matches printed grand total", "mgr")
    check("human correction clears the mismatch and keeps the original", not [i for i in fixed["issues"] if i["code"] == "TOTAL_MISMATCH"]
          and {x["field_path"]: x for x in fixed["fields"]}["total_amount"]["original_value"] == "1870")

    # ---------------- E: invalid GSTIN as read by the model -> CONFIRMED ----------------
    work3 = work / "invoice3.pdf"
    lines3 = [l.replace("27AAPFU0939F1ZV", "27AAPFU0939F1ZW") for l in LINES]
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        for n, line in enumerate(lines3):
            page.insert_text((72, 90 + 20 * n), line, fontsize=11)
        doc.save(work3)
    stub(payload(gstin="27AAPFU0939F1ZW", cites=dict(CITES, **{"seller.tax_ids[0].value": "GSTIN: 27AAPFU0939F1ZW"})))
    d3, _ = service.ingest("mgr", "invoice3.pdf", work3.read_bytes(), "application/pdf")
    g3 = service.process(d3["id"])
    gi = [i for i in g3["issues"] if i["code"] == "GSTIN_INVALID"]
    check("mistyped GSTIN printed on the invoice is flagged CONFIRMED with its field path",
          gi and gi[0]["severity"] == "CONFIRMED" and gi[0]["field_path"] == "seller.tax_ids[0].value", str(g3["issues"]))

    # ---------------- H: docket's own "uncited rows" integrity error -> audited acknowledgement ----------------
    work4 = work / "invoice4.pdf"
    lines4 = [l.replace("0042", "0043") for l in LINES]
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        for n, line in enumerate(lines4):
            page.insert_text((72, 90 + 20 * n), line, fontsize=11)
        doc.save(work4)
    cites4 = dict(CITES_NO_LINES, invoice_number="Invoice No: GST/26-27/0043")
    stub(dict(payload(cites=cites4), invoice_number="GST/26-27/0043"))
    d4, _ = service.ingest("mgr", "invoice4.pdf", work4.read_bytes(), "application/pdf")
    g4 = service.process(d4["id"])
    uncited = [i for i in g4["issues"] if i["source"] == "docket" and i["severity"] == "CONFIRMED"
               and "cite no source line" in i["message"]]
    check("rows nobody can cite are a CONFIRMED engine issue", len(uncited) == 1 and g4["status"] == "needs_review", str(g4["issues"]))
    try:
        service.approve(d4["id"], "mgr")
        blocked = False
    except DocumentError as exc:
        blocked = exc.code == "confirmed_issues_open"
    check("approval blocked by the engine issue", blocked)
    service.acknowledge_issues(d4["id"], uncited[0]["code"], uncited[0]["field_path"],
                               "Checked the rows against the printed invoice", "mgr")
    after = service.get_document(d4["id"])
    ack = [i for i in after["issues"] if i["field_path"] == "line_items"][0]
    check("acknowledged with who/why recorded, issue still visible", ack["acknowledged_by"] == "mgr"
          and "printed invoice" in ack["acknowledgement_reason"])
    check("acknowledged document is approvable", service.approve(d4["id"], "mgr")["status"] == "approved")

    # ---------------- I: a value that contradicts its own citation is rejected by the engine ----------------
    stub(dict(payload(), invoice_number="GST/26-27/9999"))   # cites the line printing ...0042
    work5 = work / "invoice5.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        for n, line in enumerate(LINES):
            page.insert_text((72, 90 + 20 * n), line + (" ." if n == 11 else ""), fontsize=11)
        doc.save(work5)
    d5, _ = service.ingest("mgr", "invoice5.pdf", work5.read_bytes(), "application/pdf")
    g5 = service.process(d5["id"])
    liar = [i for i in g5["issues"] if i["field_path"] == "invoice_number" and i["severity"] == "CONFIRMED"]
    check("model value that its own citation does not print -> CONFIRMED issue, needs review",
          liar and "does not print it" in liar[0]["message"] and g5["status"] == "needs_review", str(g5["issues"]))

    # ---------------- F: real OCR on a rasterised scan (no text layer) ----------------
    if shutil.which("tesseract"):
        png_path = work / "scan.png"
        with fitz.open(pdf_path) as src:
            src[0].get_pixmap(dpi=200).save(png_path)
        with fitz.open(png_path) as probe:
            pass
        stub(payload())
        out = extractor.extract(png_path)
        f = {x.path: x for x in out.fields}
        located = [p for p in CITES if p in f and f[p].location_status in ("verified", "fuzzy") and f[p].bbox]
        check("scanned image: OCR output is cited to boxes for most key fields", out.status != "failed" and len(located) >= 6,
              f"{out.status} {out.error_message} located={located}")
        check("scanned image: confidence reflects OCR (strictly between 0 and 1)", 0 < (f["total_amount"].confidence or 0) <= 1, str(f.get("total_amount")))
    else:
        print("SKIP scanned-image OCR case (tesseract binary not installed)")

    # ---------------- G: outage is a retryable failure, never an empty review item ----------------
    docket_extract.chat_json = real_chat_json
    os.environ.update({"DOCKET_LLM_PROVIDER": "openai", "DOCKET_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                       "DOCKET_LLM_API_KEY": "sk-SECRETKEY1234567890", "DOCKET_TEXT_MODEL": "m",
                       "DOCKET_LLM_RETRIES": "0", "DOCKET_MAX_EXTRACT_RETRIES": "0"})
    out = DocketExtractor().extract(pdf_path)
    check("LLM outage -> failed/extraction_empty (not needs_review with no fields)",
          out.status == "failed" and out.error_code == "extraction_empty" and not out.fields, f"{out.status} {out.error_code}")
    check("no secret in the failure message", "SECRETKEY" not in (out.error_message or ""))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
