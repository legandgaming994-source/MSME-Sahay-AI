"""REST surface for the MSME compliance workspace (see msme/compliance/service.py).

Viewers read; non-demo managers prepare, submit, approve and record; the service enforces maker-checker.
Nothing here files a return or contacts any government system.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from api.auth import require_non_public_manager, require_role
from msme.compliance import report
from msme.compliance.service import ComplianceError, ComplianceService

router = APIRouter(prefix="/msme/compliance", tags=["msme-compliance"])
_viewer = Depends(require_role("viewer"))
_writer = Depends(require_non_public_manager)

TASK_ID = Path(pattern=r"^MTSK-[0-9a-f]{12}$")
WP_ID = Path(pattern=r"^MWP-[0-9a-f]{12}$")


@lru_cache(maxsize=1)
def get_service() -> ComplianceService:
    return ComplianceService()


def _raise(exc: ComplianceError):
    raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


class ProfileBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)
    filer_type: str = Field(min_length=1, max_length=30)
    state_category: Optional[str] = Field(default=None, max_length=1)
    gstin: Optional[str] = Field(default=None, max_length=20)


class OverrideBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)
    form: str = Field(min_length=1, max_length=20)
    period: str = Field(min_length=1, max_length=40)
    due_date: str = Field(min_length=10, max_length=10)
    source: str = Field(min_length=5, max_length=300)


class GenerateBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)
    fy_start_year: int = Field(ge=2021, le=2100)


class ChecklistUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    status: Optional[str] = Field(default=None, max_length=10)
    note: Optional[str] = Field(default=None, max_length=500)
    document_ids: Optional[list[str]] = Field(default=None, max_length=20)


class DraftUpdate(BaseModel):
    checklist: Optional[list[ChecklistUpdate]] = Field(default=None, max_length=50)
    notes: Optional[str] = Field(default=None, max_length=2000)
    refresh_figures: bool = False


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class FilingBody(BaseModel):
    reference: str = Field(min_length=3, max_length=60)
    filed_on: str = Field(min_length=10, max_length=10)
    note: Optional[str] = Field(default=None, max_length=300)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ComplianceError as exc:
        _raise(exc)


@router.get("/profile")
def get_profile(company: str = Query(min_length=1, max_length=140), _u: dict = _viewer,
                s: ComplianceService = Depends(get_service)):
    profile = s.get_profile(company)
    if not profile:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "No profile for this company"})
    return profile


@router.put("/profile")
def set_profile(body: ProfileBody, user: dict = _writer, s: ComplianceService = Depends(get_service)):
    return _run(s.set_profile, body.company, body.filer_type, body.state_category, body.gstin, user["name"])


@router.post("/overrides")
def add_override(body: OverrideBody, user: dict = _writer, s: ComplianceService = Depends(get_service)):
    return _run(s.add_override, body.company, body.form, body.period, body.due_date, body.source, user["name"])


@router.post("/tasks/generate")
def generate_tasks(body: GenerateBody, user: dict = _writer, s: ComplianceService = Depends(get_service)):
    return _run(s.generate_tasks, body.company, body.fy_start_year, user["name"])


@router.get("/tasks")
def list_tasks(company: Optional[str] = Query(default=None, max_length=140), fy_start_year: Optional[int] = None,
               status: Optional[str] = Query(default=None, max_length=30), limit: int = Query(default=100, ge=1, le=500),
               offset: int = Query(default=0, ge=0), _u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    return s.list_tasks(company, fy_start_year, status, limit, offset)


@router.get("/upcoming")
def upcoming(company: str = Query(min_length=1, max_length=140), days: int = Query(default=14, ge=0, le=366),
             _u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    return {"tasks": s.upcoming(company, days)}


@router.get("/calendar.csv")
def calendar_csv(company: str = Query(min_length=1, max_length=140), fy_start_year: int = Query(ge=2021, le=2100),
                 _u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    rows = s.list_tasks(company, fy_start_year, None, 500, 0)["rows"]
    return Response(content=report.calendar_csv(rows), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="compliance-calendar-{fy_start_year}.csv"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/tasks/{task_id}")
def get_task(task_id: str = TASK_ID, _u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    return _run(s.get_task, task_id)


@router.post("/tasks/{task_id}/workpapers", status_code=201)
def open_workpaper(task_id: str = TASK_ID, user: dict = _writer, s: ComplianceService = Depends(get_service)):
    return _run(s.open_workpaper, task_id, user["name"])


@router.post("/tasks/{task_id}/filing")
def record_filing(body: FilingBody, task_id: str = TASK_ID, user: dict = _writer,
                  s: ComplianceService = Depends(get_service)):
    return _run(s.record_filing, task_id, user["name"], body.reference, body.filed_on, body.note)


@router.get("/workpapers/{workpaper_id}")
def get_workpaper(workpaper_id: str = WP_ID, _u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    return _run(s.get_workpaper, workpaper_id)


@router.put("/workpapers/{workpaper_id}")
def update_workpaper(body: DraftUpdate, workpaper_id: str = WP_ID, user: dict = _writer,
                     s: ComplianceService = Depends(get_service)):
    checklist = [c.model_dump(exclude_unset=True) for c in body.checklist] if body.checklist is not None else None
    return _run(s.update_draft, workpaper_id, user["name"], checklist, body.notes, body.refresh_figures)


@router.post("/workpapers/{workpaper_id}/submit")
def submit_workpaper(workpaper_id: str = WP_ID, user: dict = _writer, s: ComplianceService = Depends(get_service)):
    return _run(s.submit_workpaper, workpaper_id, user["name"])


@router.post("/workpapers/{workpaper_id}/approve")
def approve_workpaper(workpaper_id: str = WP_ID, user: dict = _writer, s: ComplianceService = Depends(get_service)):
    return _run(s.approve_workpaper, workpaper_id, user["name"])


@router.post("/workpapers/{workpaper_id}/reject")
def reject_workpaper(body: RejectBody, workpaper_id: str = WP_ID, user: dict = _writer,
                     s: ComplianceService = Depends(get_service)):
    return _run(s.reject_workpaper, workpaper_id, user["name"], body.reason)


@router.get("/workpapers/{workpaper_id}/report.pdf")
def workpaper_report(workpaper_id: str = WP_ID, _u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    pdf = _run(report.workpaper_pdf, s, workpaper_id)
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="workpaper-{workpaper_id}.pdf"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/audit")
def audit_events(entity_id: Optional[str] = Query(default=None, max_length=40), limit: int = Query(default=100, ge=1, le=500),
                 offset: int = Query(default=0, ge=0), _u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    return {"events": s.audit_events(entity_id, limit, offset)}


@router.get("/audit/verify")
def verify_audit(_u: dict = _viewer, s: ComplianceService = Depends(get_service)):
    return s.verify_audit()
