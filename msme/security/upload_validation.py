"""Content-based validation for untrusted uploads (S-2).

Never trusts the client-declared Content-Type or filename. The file's bytes
decide what it is; the declared type and extension are only used to choose
between *compatible* interpretations (e.g. CSV mislabelled by a browser as
``application/vnd.ms-excel``).

Residual risk (documented): this is structural validation, not malware
scanning. Downstream parsers (OCR, openpyxl, LLM providers) must still treat
content as hostile.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Iterable, Mapping, Optional
from urllib.parse import quote

MAX_ZIP_MEMBERS = 1000
MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
_STREAM_CHUNK = 1024 * 1024

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ODF_MIMES = {
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.presentation",
}
OOXML_MARKERS = {XLSX: "xl/workbook.xml", DOCX: "word/document.xml", PPTX: "ppt/presentation.xml"}
OLE_MIMES = {"application/vnd.ms-excel", "application/msword", "application/vnd.ms-powerpoint"}
ZIP_MIMES = {"application/zip", "application/x-zip-compressed"}
XML_MIMES = {"application/xml", "text/xml"}
TEXT_MIMES = {"text/csv", "text/plain"}
INLINE_SAFE_MIMES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_EXEC_PREFIXES = (b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                  b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe")
_ACTIVE_XML = re.compile(rb"<!DOCTYPE|<!ENTITY|<script|xmlns\s*=\s*[\"']http://www\.w3\.org/(1999/xhtml|2000/svg)|"
                         rb"<\?xml-stylesheet", re.IGNORECASE)


class UploadRejected(ValueError):
    """The upload failed content validation; the message is safe to show the user."""


def _strip_bom_ws(data: bytes) -> bytes:
    head = data[:4096]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    return head.lstrip(b" \t\r\n")


def _check_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadRejected("Corrupt or invalid ZIP container.") from exc
    infos = zf.infolist()
    if not infos:
        raise UploadRejected("Empty ZIP container.")
    if len(infos) > MAX_ZIP_MEMBERS:
        raise UploadRejected(f"ZIP has too many entries (limit {MAX_ZIP_MEMBERS}).")
    declared = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/") or ":" in name.split("/")[0]:
            raise UploadRejected("ZIP contains an unsafe path.")
        if info.flag_bits & 0x1:
            raise UploadRejected("Encrypted ZIP entries are not supported.")
        declared += info.file_size
    if declared > MAX_ZIP_EXPANDED_BYTES:
        raise UploadRejected("ZIP expands beyond the allowed size.")
    # Central-directory sizes can be forged: count the bytes actually produced.
    actual = 0
    for info in infos:
        if info.is_dir():
            continue
        try:
            with zf.open(info) as member:
                while chunk := member.read(_STREAM_CHUNK):
                    actual += len(chunk)
                    if actual > MAX_ZIP_EXPANDED_BYTES:
                        raise UploadRejected("ZIP expands beyond the allowed size.")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise UploadRejected("Corrupt or unsupported ZIP entry.") from exc
    return zf


def _detect(data: bytes) -> tuple[str, Optional[str]]:
    """Return (kind, specific_mime). kind in pdf/png/jpeg/gif/webp/zip/ole/xml/text/executable."""
    if data.startswith(_EXEC_PREFIXES):
        return "executable", None
    if data.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data.startswith(_OLE_MAGIC):
        return "ole", None
    if data.startswith(b"PK\x03\x04"):
        zf = _check_zip(data)
        names = set(zf.namelist())
        if "[Content_Types].xml" in names:
            for mime, marker in OOXML_MARKERS.items():
                if marker in names:
                    return "zip", mime
        if "mimetype" in names:
            try:
                declared = zf.read("mimetype").decode("ascii", "ignore").strip()
            except Exception:  # noqa: BLE001
                declared = ""
            if declared in ODF_MIMES:
                return "zip", declared
        return "zip", None
    stripped = _strip_bom_ws(data)
    if stripped.startswith(b"<"):
        return "xml", None
    sample = data[: 64 * 1024]
    if b"\x00" in sample:
        # UTF-16 text with BOM is legitimate for some exports; everything else with NUL is binary.
        if not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
            return "binary", None
    controls = sum(1 for b in sample if b < 9 or 13 < b < 32)
    if sample and controls / len(sample) > 0.01:
        return "binary", None
    return "text", None


def validate_upload(
    declared_mime: str,
    filename: str,
    data: bytes,
    allowed_mimes: Iterable[str],
    ext_to_mime: Mapping[str, str],
) -> str:
    """Return the canonical MIME type for ``data`` or raise :class:`UploadRejected`."""
    allowed = set(allowed_mimes)
    declared = (declared_mime or "").lower()
    ext = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    ext_mime = ext_to_mime.get(ext)
    kind, specific = _detect(data)
    if kind in ("executable", "binary"):
        raise UploadRejected("This file's contents are not an accepted document type.")

    mismatch = UploadRejected("File contents do not match the declared file type.")
    declared_specific = declared in allowed          # a real, allowed claim (not octet-stream/unknown)
    ext_specific = ext_mime in allowed if ext_mime else False
    office_family = set(OOXML_MARKERS) | ODF_MIMES | OLE_MIMES

    def claim_must_be(*compatible: str) -> Optional[str]:
        """Return the matching claim; raise if a specific claim contradicts the content."""
        if declared_specific:
            if declared in compatible:
                return declared
            raise mismatch
        if ext_specific:
            if ext_mime in compatible:
                return ext_mime
            raise mismatch
        return None

    if kind in ("pdf", "png", "jpeg", "gif", "webp"):
        claim_must_be(specific)  # type: ignore[arg-type]
        return specific  # type: ignore[return-value]
    if kind == "ole":
        claim = claim_must_be(*OLE_MIMES)
        if claim is None:
            raise UploadRejected("Cannot determine the legacy Office file type; use a .xls/.doc/.ppt extension.")
        return claim
    if kind == "zip":
        if specific:  # Office/ODF container: the content decides the canonical type
            if declared_specific and declared not in ({specific} | ZIP_MIMES | office_family):
                raise mismatch
            if declared in ZIP_MIMES:
                return declared
            return specific
        claim = claim_must_be(*ZIP_MIMES)
        return claim or "application/zip"
    if kind == "xml":
        if _ACTIVE_XML.search(data):
            raise UploadRejected("XML with DOCTYPE/entities, scripts or XHTML/SVG content is not accepted.")
        claim = claim_must_be(*XML_MIMES)
        return claim or "application/xml"
    if kind == "text":
        # Browsers often label CSV as application/vnd.ms-excel; accept when the extension says CSV/TXT.
        if declared in TEXT_MIMES:
            return declared
        if ext_mime in TEXT_MIMES and (declared in OLE_MIMES or not declared_specific):
            return ext_mime
        if declared_specific:
            raise mismatch
        return "text/plain"
    raise UploadRejected("Unrecognised file contents.")


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def safe_content_disposition(filename: str, mime: str) -> str:
    """Content-Disposition that cannot be broken out of; inline only for passive types."""
    base = (filename or "file").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable())[:150] or "file"
    ascii_name = _SAFE_NAME.sub("_", base) or "file"
    disposition = "inline" if mime in INLINE_SAFE_MIMES else "attachment"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(base, safe='')}"
