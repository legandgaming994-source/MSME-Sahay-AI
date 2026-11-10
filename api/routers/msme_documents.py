"""REST surface for MSME document intelligence (see msme/documents/service.py).

Roles: viewers read; non-demo managers upload, process, correct and approve; interactive admins sweep.
Every operation is authorised server-side; nothing relies on client-side checks.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Path, Query, Response, UploadFile
from pydantic import BaseModel, Field

from api.auth import require_interactive_admin, require_non_public_manager, require_role
from msme.documents.extraction import DocketExtractor
from msme.documents.service import MAX_BYTES, DocumentError, DocumentService
from msme.documents.storage import get_storage
from msme.security.upload_validation import safe_content_disposition

router = APIRouter(prefix="/msme/documents", tags=["msme-documents"])
_viewer = Depends(require_role("viewer"))
_writer = Depends(require_non_public_manager)
_admin = Depends(require_interactive_admin)

DOC_ID = Path(pattern=r"^MDOC-[0-9a-f]{12}$")


@lru_cache(maxsize=1)
def get_service() -> DocumentService:
    """Process-wide service. Tests override this dependency with a fake extractor."""
    return DocumentService(DocketExtractor(), get_storage())


def _raise(exc: DocumentError):
    raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


class FieldCorrection(BaseModel):
    field_path: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)


class IssueAcknowledgement(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    field_path: Optional[str] = Field(default=None, max_length=120)
    reason: str = Field(min_length=1, max_length=500)


@router.post("", status_code=201)
async def upload_document(
    background: BackgroundTasks,
    response: Response,
    file: UploadFile = File(...),
    process: bool = Query(default=True),
    user: dict = _writer,
    service: DocumentService = Depends(get_service),
):
    data = await file.read(MAX_BYTES + 1)
    try:
        document, duplicate = service.ingest(user["name"], file.filename or "", data, file.content_type or "")
    except DocumentError as exc:
        _raise(exc)
    if duplicate:
        response.status_code = 200
    elif process:
        background.add_task(service.process, document["id"])
    return {"duplicate": duplicate, "document": document}


@router.get("")
def list_documents(status: Optional[str] = None, limit: int = Query(default=50, ge=1, le=200),
                   offset: int = Query(default=0, ge=0), _user: dict = _viewer,
                   service: DocumentService = Depends(get_service)):
    return service.list_documents(status, limit, offset)


@router.post("/sweep")
def sweep(limit: int = Query(default=10, ge=1, le=100), _user: dict = _admin,
          service: DocumentService = Depends(get_service)):
    return service.sweep(limit)


@router.get("/{document_id}")
def get_document(document_id: str = DOC_ID, _user: dict = _viewer, service: DocumentService = Depends(get_service)):
    try:
        return service.get_document(document_id)
    except DocumentError as exc:
        _raise(exc)


@router.get("/{document_id}/file")
def download_original(document_id: str = DOC_ID, _user: dict = _viewer,
                      service: DocumentService = Depends(get_service)):
    try:
        data, mime, filename = service.read_original(document_id)
    except DocumentError as exc:
        _raise(exc)
    return Response(content=data, media_type=mime, headers={
        "Content-Disposition": safe_content_disposition(filename, mime),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/{document_id}/history")
def correction_history(document_id: str = DOC_ID, field_path: Optional[str] = Query(default=None, max_length=120),
                       _user: dict = _viewer, service: DocumentService = Depends(get_service)):
    try:
        return {"corrections": service.history(document_id, field_path)}
    except DocumentError as exc:
        _raise(exc)


@router.post("/{document_id}/process")
def process_document(document_id: str = DOC_ID, force: bool = Query(default=False), _user: dict = _writer,
                     service: DocumentService = Depends(get_service)):
    try:
        return service.process(document_id, force=force)
    except DocumentError as exc:
        _raise(exc)


@router.put("/{document_id}/fields")
def correct_field(body: FieldCorrection, document_id: str = DOC_ID, user: dict = _writer,
                  service: DocumentService = Depends(get_service)):
    try:
        return service.correct_field(document_id, body.field_path, body.value, body.reason, user["name"])
    except DocumentError as exc:
        _raise(exc)


@router.post("/{document_id}/approve")
def approve_document(document_id: str = DOC_ID, user: dict = _writer,
                     service: DocumentService = Depends(get_service)):
    try:
        return service.approve(document_id, user["name"])
    except DocumentError as exc:
        _raise(exc)


@router.post("/{document_id}/issues/acknowledge")
def acknowledge_issue(body: IssueAcknowledgement, document_id: str = DOC_ID, user: dict = _writer,
                      service: DocumentService = Depends(get_service)):
    try:
        return service.acknowledge_issues(document_id, body.code, body.field_path, body.reason, user["name"])
    except DocumentError as exc:
        _raise(exc)
