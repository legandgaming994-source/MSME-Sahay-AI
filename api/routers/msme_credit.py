"""REST surface for credit-readiness and scheme navigation (see msme/credit/service.py).

Viewers read; non-demo managers enter facts, attest documents and run persisted assessments.
Nothing here contacts a lender, a scheme administrator or any government system.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from api.auth import require_non_public_manager, require_role
from msme.credit import report
from msme.credit.service import CreditError, CreditService

router = APIRouter(prefix="/msme/credit", tags=["msme-credit"])
_viewer = Depends(require_role("viewer"))
_writer = Depends(require_non_public_manager)
COMPANY = Query(min_length=1, max_length=140)
SCHEME = Path(pattern=r"^[a-z0-9\-]{2,40}$")


@lru_cache(maxsize=1)
def get_service() -> CreditService:
    return CreditService()


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except CreditError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


class FactBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)
    key: str = Field(min_length=1, max_length=60)
    value: Any
    evidence_document_id: Optional[str] = Field(default=None, pattern=r"^MDOC-[0-9a-f]{12}$")


class AttestBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)
    item_key: str = Field(min_length=1, max_length=60)
    status: str = Field(min_length=1, max_length=20)
    note: str = Field(min_length=1, max_length=300)
    document_id: Optional[str] = Field(default=None, pattern=r"^MDOC-[0-9a-f]{12}$")


class AssessBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)


@router.get("/facts")
def get_facts(company: str = COMPANY, _u: dict = _viewer, s: CreditService = Depends(get_service)):
    return _run(s.get_facts, company)


@router.put("/facts")
def set_fact(body: FactBody, user: dict = _writer, s: CreditService = Depends(get_service)):
    return _run(s.set_fact, body.company, body.key, body.value, user["name"], body.evidence_document_id)


@router.get("/facts/history")
def fact_history(company: str = COMPANY, key: str = Query(min_length=1, max_length=60), _u: dict = _viewer,
                 s: CreditService = Depends(get_service)):
    return {"history": _run(s.fact_history, company, key)}


@router.get("/readiness")
def readiness(company: str = COMPANY, _u: dict = _viewer, s: CreditService = Depends(get_service)):
    return _run(s.readiness, company)


@router.put("/readiness/attestations")
def attest(body: AttestBody, user: dict = _writer, s: CreditService = Depends(get_service)):
    return _run(s.attest, body.company, body.item_key, body.status, body.note, body.document_id, user["name"])


@router.get("/turnover-check")
def turnover_check(company: str = COMPANY, _u: dict = _viewer, s: CreditService = Depends(get_service)):
    return _run(s.turnover_check, company)


@router.get("/schemes")
def schemes(_u: dict = _viewer, s: CreditService = Depends(get_service)):
    from datetime import datetime, timezone
    return {"schemes": s.catalogue(datetime.now(timezone.utc).date())}


@router.post("/schemes/assess")
def assess(body: AssessBody, user: dict = _writer, s: CreditService = Depends(get_service)):
    return _run(s.assess, body.company, user["name"])


@router.get("/schemes/assessments")
def assessments(company: str = COMPANY, scheme_id: Optional[str] = Query(default=None, max_length=40),
                limit: int = Query(default=50, ge=1, le=200), _u: dict = _viewer,
                s: CreditService = Depends(get_service)):
    return {"assessments": _run(s.assessments, company, scheme_id, limit)}


@router.get("/schemes/{scheme_id}/summary")
def summary(scheme_id: str = SCHEME, company: str = COMPANY, _u: dict = _viewer, s: CreditService = Depends(get_service)):
    return _run(s.application_summary, company, scheme_id)


@router.get("/schemes/{scheme_id}/summary.pdf")
def summary_pdf(scheme_id: str = SCHEME, company: str = COMPANY, _u: dict = _viewer,
                s: CreditService = Depends(get_service)):
    pdf = report.summary_pdf(_run(s.application_summary, company, scheme_id))
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="application-summary-{scheme_id}.pdf"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
