"""Pure unit tests for msme.security.upload_validation (no DB / no app)."""

import io
import zipfile

import pytest

from api.attachments import ALLOWED_MIME_TYPES, _EXT_TO_MIME
from msme.security.upload_validation import UploadRejected, safe_content_disposition, validate_upload

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64


def v(mime, name, data):
    return validate_upload(mime, name, data, ALLOWED_MIME_TYPES, _EXT_TO_MIME)


def _xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _odt():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", "<x/>")
    return buf.getvalue()


def test_mislabelled_legit_files_still_accepted():
    assert v("application/vnd.ms-excel", "rows.csv", b"a,b\n1,2\n") == "text/csv"          # browser CSV mislabel
    assert v("application/octet-stream", "book.xlsx", _xlsx()) == XLSX                       # generic label
    assert v("application/vnd.ms-excel", "book.xlsx", _xlsx()) == XLSX                       # same Office family
    assert v("application/octet-stream", "old.xls", OLE) == "application/vnd.ms-excel"
    assert v("application/octet-stream", "d.odt", _odt()) == "application/vnd.oasis.opendocument.text"
    assert v("image/jpeg", "p.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32) == "image/jpeg"
    assert v("text/plain", "n.txt", b"\xef\xbb\xbfhello") == "text/plain"


def test_contradicting_specific_claims_rejected():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    with pytest.raises(UploadRejected):
        v("application/pdf", "x.pdf", png)
    with pytest.raises(UploadRejected):
        v("image/png", "x.png", b"%PDF-1.7\n")
    with pytest.raises(UploadRejected):
        v("application/octet-stream", "photo.pdf", png)  # generic label, contradicting extension


def test_unknown_ole_without_claim_rejected():
    with pytest.raises(UploadRejected):
        v("application/octet-stream", "mystery.bin", OLE)


def test_executables_and_scripts_rejected_even_when_text_like():
    for payload in (b"MZ\x90\x00", b"\x7fELF\x02", b"#!/bin/sh\nrm -rf /\n"):
        with pytest.raises(UploadRejected):
            v("text/plain", "run.txt", payload)


def test_zip_slip_and_encrypted_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(UploadRejected):
        v("application/zip", "a.zip", buf.getvalue())


def test_content_disposition_shapes():
    h = safe_content_disposition('a"; x=1\r\n.pdf', "application/pdf")
    assert h.startswith("inline;") and "\r" not in h and "\n" not in h and h.count('"') == 2
    assert safe_content_disposition("r\u00e9sum\u00e9.xml", "application/xml").startswith("attachment;")
    assert "UTF-8''r%C3%A9sum%C3%A9.xml" in safe_content_disposition("r\u00e9sum\u00e9.xml", "application/xml")
    assert safe_content_disposition("", "text/csv").startswith("attachment; filename=\"file\"")
