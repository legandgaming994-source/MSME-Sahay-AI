#!/usr/bin/env python3
"""S-2 regression: chat-attachment upload content validation and safe download.

Covers: declared-type vs magic-byte mismatch, executables, XXE/DOCTYPE in XML,
zip bombs, spoofed Office containers, user-controlled extension on disk,
Content-Disposition injection, inline serving of active content, nosniff.

Run:  python -m tests.test_upload_security
"""

import io
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

FIXTURE_XML = Path(__file__).parent / "fixtures" / "camt053_synthetic.xml"

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 20
HTML = b"<html><body><script>fetch('/api/auth/me')</script></body></html>"


def _xlsx_bytes() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active["A1"] = "synthetic"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _zip(members: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def main() -> int:
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="lambda_upload_sec_")
    os.close(fd)
    upload_root = tempfile.mkdtemp(prefix="lambda_uploads_")
    os.environ.update({
        "LAMBDA_ERP_DB": db_path, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
        "JWT_SECRET_KEY": "synthetic-upload-security-secret", "OPENAI_API_KEY": "synthetic-not-used",
    })

    from fastapi.testclient import TestClient
    from api.main import app
    from lambda_erp.database import get_db

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    with TestClient(app) as client:
        r = client.post("/api/auth/register", json={
            "email": "admin@synthetic.invalid", "full_name": "Admin", "password": "synthetic-pass-12345"})
        assert r.status_code == 200, r.text
        sid = client.post("/api/chat/sessions").json()["id"]

        def up(filename, data, mime):
            return client.post("/api/chat/attachments", files={"file": (filename, data, mime)}, data={"session_id": sid})

        # ---- must be rejected --------------------------------------------
        r = up("invoice.pdf", HTML, "application/pdf")
        check("html body declared as pdf -> 400", r.status_code == 400, f"{r.status_code}")
        r = up("logo.png", b"MZ\x90\x00" + b"\x00" * 64, "image/png")
        check("windows executable declared as png -> 400", r.status_code == 400, f"{r.status_code}")
        r = up("x.png", b"\x7fELF" + b"\x00" * 64, "image/png")
        check("elf binary declared as png -> 400", r.status_code == 400, f"{r.status_code}")
        r = up("bad.pdf", PNG, "application/pdf")
        check("png body declared as pdf -> 400", r.status_code == 400, f"{r.status_code}")
        xxe = (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><d>&x;</d>')
        r = up("s.xml", xxe, "application/xml")
        check("xml with DOCTYPE/ENTITY -> 400", r.status_code == 400, f"{r.status_code}")
        r = up("page.xml", b'<html xmlns="http://www.w3.org/1999/xhtml"><script>1</script></html>', "text/xml")
        check("xhtml-like active xml -> 400", r.status_code == 400, f"{r.status_code}")
        r = up("fake.docx", _zip({"hello.txt": b"hi"}), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        check("plain zip declared as docx -> 400", r.status_code == 400, f"{r.status_code}")
        bomb = _zip({"a.bin": b"\x00" * (200 * 1024 * 1024)})
        r = up("bomb.zip", bomb, "application/zip")
        check("zip bomb (200MB expanded) -> 400", r.status_code == 400, f"{r.status_code}")
        many = _zip({f"f{i}.txt": b"x" for i in range(3000)})
        r = up("many.zip", many, "application/zip")
        check("zip with 3000 members -> 400", r.status_code == 400, f"{r.status_code}")
        r = up("nul.csv", b"a,b\n1,2\x00\x00\x00\n", "text/csv")
        check("binary NUL bytes declared as csv -> 400", r.status_code == 400, f"{r.status_code}")

        # ---- must still be accepted --------------------------------------
        for label, fn, data, mime in [
            ("valid pdf", "invoice.pdf", PDF, "application/pdf"),
            ("valid png", "logo.png", PNG, "image/png"),
            ("valid csv", "rows.csv", b"a,b\n1,2\n", "text/csv"),
            ("valid txt (utf-8)", "n.txt", "caf\u00e9".encode(), "text/plain"),
            ("valid xlsx", "book.xlsx", _xlsx_bytes(), "application/octet-stream"),
            ("valid camt xml", FIXTURE_XML.name, FIXTURE_XML.read_bytes(), "application/xml"),
            ("valid zip (bank export)", "b.zip", _zip({"s.xml": FIXTURE_XML.read_bytes()}), "application/zip"),
        ]:
            r = up(fn, data, mime)
            check(f"{label} accepted", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

        # ---- storage / download hardening --------------------------------
        r = up("evil.html", PDF, "application/pdf")
        ok = r.status_code == 200
        check("pdf with .html filename accepted", ok, f"{r.status_code}")
        if ok:
            att = get_db().sql('SELECT file_path, mime_type FROM "Chat Attachment" WHERE id = ?', [r.json()["id"]])[0]
            check("stored extension derives from detected type (.pdf), not user filename",
                  att["file_path"].endswith(".pdf"), att["file_path"])
            d = client.get(f"/api/chat/attachments/{r.json()['id']}")
            check("download sets X-Content-Type-Options: nosniff",
                  d.headers.get("x-content-type-options", "").lower() == "nosniff", str(dict(d.headers)))

        r = up('a"; filename*=UTF-8\'\'evil.html;.pdf', PDF, "application/pdf")
        if r.status_code == 200:
            d = client.get(f"/api/chat/attachments/{r.json()['id']}")
            cd = d.headers.get("content-disposition", "")
            check("filename cannot break out of Content-Disposition quoting",
                  cd.count('"') == 2 and "evil.html;" not in cd.split("filename=")[-1].strip('"'), cd)
        else:
            check("hostile filename handled (accepted+sanitised or rejected)", r.status_code == 400, f"{r.status_code}")

        r = up("s.xml", FIXTURE_XML.read_bytes(), "application/xml")
        if r.status_code == 200:
            d = client.get(f"/api/chat/attachments/{r.json()['id']}")
            check("xml is served as attachment, never inline",
                  d.headers.get("content-disposition", "").lower().startswith("attachment"),
                  d.headers.get("content-disposition", ""))
        r = up("p.pdf", PDF, "application/pdf")
        d = client.get(f"/api/chat/attachments/{r.json()['id']}")
        check("pdf may still render inline",
              d.headers.get("content-disposition", "").lower().startswith("inline"),
              d.headers.get("content-disposition", ""))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
