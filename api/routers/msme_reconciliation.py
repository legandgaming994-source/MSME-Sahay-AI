"""REST surface for document <-> bank-transaction reconciliation (see msme/reconciliation/service.py).

Viewers read; non-demo managers run the matcher and confirm, reject, unmatch or match manually. Bank data is
never modified by these routes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from api.auth import require_non_public_manager, require_role
from msme.reconciliation.service import ReconciliationService, ReconError

router = APIRouter(prefix="/msme/reconciliation", tags=["msme-reconciliation"])
_viewer = Depends(require_role("viewer"))
_writer = Depends(require_non_public_manager)
COMPANY = Query(min_length=1, max_length=140)
MATCH_ID = Path(pattern=r"^MRCM-[0-9a-f]{12}$")


@lru_cache(maxsize=1)
def get_service() -> ReconciliationService:
    return ReconciliationService()


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ReconError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": exc.message})


class RunBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)


class ConfirmBody(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class ManualBody(BaseModel):
    company: str = Field(min_length=1, max_length=140)
    document_id: str = Field(pattern=r"^MDOC-[0-9a-f]{12}$")
    bank_transaction: str = Field(min_length=1, max_length=140)
    note: str = Field(min_length=1, max_length=300)


@router.post("/run")
def run(body: RunBody, user: dict = _writer, s: ReconciliationService = Depends(get_service)):
    return _run(s.run, body.company, user["name"])


@router.get("/matches")
def matches(company: str = COMPANY, status: Optional[str] = Query(default=None, max_length=20),
            limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), _u: dict = _viewer,
            s: ReconciliationService = Depends(get_service)):
    return _run(s.list_matches, company, status, limit, offset)


@router.post("/matches/manual", status_code=201)
def manual(body: ManualBody, user: dict = _writer, s: ReconciliationService = Depends(get_service)):
    return _run(s.manual_match, body.company, body.document_id, body.bank_transaction, user["name"], body.note)


@router.get("/matches/{match_id}")
def get_match(match_id: str = MATCH_ID, _u: dict = _viewer, s: ReconciliationService = Depends(get_service)):
    return _run(s.get_match, match_id)


@router.post("/matches/{match_id}/confirm")
def confirm(body: ConfirmBody, match_id: str = MATCH_ID, user: dict = _writer, s: ReconciliationService = Depends(get_service)):
    return _run(s.confirm, match_id, user["name"], body.note)


@router.post("/matches/{match_id}/reject")
def reject(body: ReasonBody, match_id: str = MATCH_ID, user: dict = _writer, s: ReconciliationService = Depends(get_service)):
    return _run(s.reject, match_id, user["name"], body.reason)


@router.post("/matches/{match_id}/unmatch")
def unmatch(body: ReasonBody, match_id: str = MATCH_ID, user: dict = _writer, s: ReconciliationService = Depends(get_service)):
    return _run(s.unmatch, match_id, user["name"], body.reason)


@router.get("/unmatched/{kind}")
def unmatched(kind: str = Path(pattern=r"^(documents|transactions)$"), company: str = COMPANY,
              limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), _u: dict = _viewer,
              s: ReconciliationService = Depends(get_service)):
    return _run(s.unmatched, company, kind, limit, offset)


@router.get("/summary")
def summary(company: str = COMPANY, _u: dict = _viewer, s: ReconciliationService = Depends(get_service)):
    return _run(s.summary, company)
