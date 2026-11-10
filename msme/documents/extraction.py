"""Extraction boundary.

`Extractor` is the only seam the rest of the system depends on. `DocketExtractor` adapts the
docket-idp pipeline (OCR/text layer -> model extraction -> citation resolution -> schema validation)
to plain dataclasses, so docket types never leak into the database or API layers and the engine can
be swapped or faked in tests.

Honesty rules enforced here:
  * a value with no resolvable source citation is recorded with location_status 'none'/'unlocated'
    and confidence None; it is never given a made-up confidence;
  * extractor failures are returned as a failed outcome with a short, secret-free message.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Protocol

SEVERITY_CONFIRMED = "CONFIRMED"
SEVERITY_HEURISTIC = "HEURISTIC"

_MAX_MESSAGE = 300
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|api[_-]?key\S*\s*[=:]\s*\S+)", re.I)


@dataclass(frozen=True)
class ExtractedField:
    path: str
    value: Optional[str]
    confidence: Optional[float] = None
    page: Optional[int] = None
    quote: Optional[str] = None
    bbox: Optional[dict] = None
    location_status: str = "none"   # verified | fuzzy | conflicting | unlocated | none


@dataclass(frozen=True)
class ExtractionIssue:
    code: str
    severity: str
    message: str
    field_path: Optional[str] = None


@dataclass
class ExtractionOutcome:
    status: str                      # extracted | needs_review | failed
    document_type: Optional[str] = None
    fields: list[ExtractedField] = field(default_factory=list)
    issues: list[ExtractionIssue] = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    extractor: str = "unknown"
    extractor_version: str = "unknown"


class Extractor(Protocol):
    name: str
    version: str

    def extract(self, path: Path) -> ExtractionOutcome: ...


def scrub(message: str) -> str:
    """Short, single-line, secret-redacted text safe to persist and show."""
    text = _SECRETISH.sub("[redacted]", " ".join(str(message).split()))
    return text[:_MAX_MESSAGE]


def _scalar(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value)


def flatten(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested extraction output into docket-style paths: ``a.b``, ``items[0].total``."""
    out: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).startswith("_") or key == "field_locations":
                continue
            out.extend(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            out.extend(flatten(value, f"{prefix}[{index}]"))
    else:
        text = _scalar(data)
        if text is not None and prefix:
            out.append((prefix, text))
    return out


class DocketExtractor:
    name = "docket-idp"

    def __init__(self, options: Any | None = None):
        self._options = options
        try:
            from docket import __version__ as _v
        except Exception:  # noqa: BLE001 - reported at extract() time
            _v = "unavailable"
        self.version = _v

    def _default_options(self):
        import os

        from docket.options import OcrOptions, ProcessOptions, ReviewOptions
        # The platform keeps its own review state; docket's queue stays off. Docket's default OCR fallback is
        # a vision model; enabling it without one configured turns every low-quality page into a confusing
        # "vlm unavailable" failure, so it is opt-in via DOCKET_VISION_MODEL.
        fallbacks = ["vlm"] if os.environ.get("DOCKET_VISION_MODEL") else []
        return ProcessOptions(ocr=OcrOptions(fallbacks=fallbacks), review=ReviewOptions(enqueue=False))

    def extract(self, path: Path) -> ExtractionOutcome:
        meta = {"extractor": self.name, "extractor_version": self.version}
        try:
            from docket import process_document
        except Exception as exc:  # noqa: BLE001
            return ExtractionOutcome("failed", error_code="extractor_not_installed",
                                     error_message=scrub(f"docket-idp is not installed: {exc}"), **meta)
        try:
            result = process_document(path, self._options or self._default_options())
        except Exception as exc:  # noqa: BLE001 - map every engine failure to a failed outcome
            code = {"BackendUnavailable": "ocr_unavailable", "ConfigurationError": "extractor_not_configured",
                    "LLMError": "llm_error"}.get(type(exc).__name__, "extractor_error")
            return ExtractionOutcome("failed", error_code=code, error_message=scrub(exc), **meta)

        status_value = getattr(result.status, "value", str(result.status))
        if status_value == "failed":
            err = result.error
            return ExtractionOutcome("failed", result.document_type,
                                     error_code=(err.code if err else "extraction_failed"),
                                     error_message=scrub(err.message if err else "Extraction failed"), **meta)

        sources = result.field_sources or {}
        fields: list[ExtractedField] = []
        for path_key, value in flatten(result.extracted or {}):
            src = sources.get(path_key)
            if src is None:
                fields.append(ExtractedField(path_key, value))
                continue
            bbox = src.bbox.model_dump() if src.bbox is not None else None
            fields.append(ExtractedField(
                path_key, value,
                confidence=(float(src.confidence) if src.confidence is not None else None),
                page=src.page, quote=src.quote, bbox=bbox, location_status=src.status))

        issues = [
            ExtractionIssue(
                code="DOCKET_" + ("ERROR" if i.severity == "error" else "WARNING"),
                severity=SEVERITY_CONFIRMED if i.severity == "error" else SEVERITY_HEURISTIC,
                message=scrub(i.message), field_path=i.field or None)
            for i in (result.validation_issues or [])
        ]
        if not fields:
            # Docket reports an LLM/model outage as 'needs_review' with nothing extracted. That is a failure
            # (retryable), not a review task: surface it so retries and the sweep handle it.
            reason = "; ".join(i.message for i in issues) or "No fields could be extracted"
            return ExtractionOutcome("failed", result.document_type, error_code="extraction_empty",
                                     error_message=scrub(reason), **meta)
        outcome_status = "needs_review" if status_value == "needs_review" else "extracted"
        return ExtractionOutcome(outcome_status, result.document_type, fields, issues, **meta)


def bbox_to_json(bbox: Optional[dict]) -> Optional[str]:
    return json.dumps(bbox, separators=(",", ":")) if bbox else None
