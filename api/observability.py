"""Structured logging, request correlation ids and a safe global error handler.

  * One JSON line per request (method, route template, status, duration, request id). Query strings, headers, cookies
    and bodies are never logged.
  * `X-Request-ID` is honoured when it is a safe token (8-64 chars of [A-Za-z0-9._-]); otherwise a new id is generated.
    The id is echoed on every response and included in every log line and in 500 responses, so a user-reported id can be
    found in the logs.
  * Anything that looks like a credential (URL passwords, bearer tokens, API keys, `password=`/`token=` pairs) is redacted
    from log messages and stack traces before they are written.
  * Unhandled exceptions return a generic JSON 500 with the request id; the details go to the log only.

Logs go to stderr as JSON (platforms such as Render capture stdout and stderr). Level from LOG_LEVEL (default INFO).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

REQUEST_ID_HEADER = b"x-request-id"
_VALID_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
QUIET_PATHS = {"/api/health", "/api/ready"}
SLOW_MS = 5000
_EXTRA_FIELDS = ("event", "method", "path", "status", "duration_ms", "error_type")

_REDACTIONS = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s/]+@"), r"\1[redacted]@"),                    # URL credentials
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer [redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "[redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|authorization|sig|accountkey)(\s*[=:]\s*)[^\s,;&\"']+"),
     r"\1\2[redacted]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
            payload["stack"] = redact(self.formatException(record.exc_info))[-4000:]
        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging(level: str | None = None) -> None:
    """Idempotent. Installs the JSON handler on the root logger and silences uvicorn's unstructured access log."""
    root = logging.getLogger()
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root.setLevel(getattr(logging, resolved, logging.INFO))
    if not any(getattr(h, "_msme_json", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler._msme_json = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    logging.getLogger("uvicorn.access").disabled = True   # replaced by the request log line below
    # HTTP client libraries log full request URLs (including query strings) at INFO. Outbound calls here go to OAuth
    # providers, LLM APIs and object stores, so keep those loggers quiet unless something is wrong.
    for noisy in ("httpx", "httpx2", "httpcore", "azure", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger("api.request")


def _route_template(scope) -> str:
    """The request path with matched path parameters replaced by their names (`/api/msme/documents/{document_id}`).
    Keeps log cardinality low and never writes record ids; derived from the path itself, so it does not depend on how
    the framework exposes the matched route."""
    path = str(scope.get("path", ""))[:200]
    params = scope.get("path_params") or {}
    if not params:
        return path
    segments = path.split("/")
    values = {str(v): k for k, v in params.items()}
    return "/".join("{" + values[seg] + "}" if seg in values else seg for seg in segments)


class RequestContextMiddleware:
    """Pure ASGI middleware (no body buffering, safe for streaming and WebSocket upgrades)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers") or []).get(REQUEST_ID_HEADER, b"").decode("latin-1")
        request_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        scope["msme_request_id"] = request_id
        token = request_id_var.set(request_id)
        started, status = time.perf_counter(), [500]

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
                message["headers"] = [*message.get("headers", []), (REQUEST_ID_HEADER, request_id.encode())]
            await send(message)

        failed = False
        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            failed = True
            log.error("unhandled exception", exc_info=True, extra={"event": "unhandled_exception"})
            raise
        finally:
            path = _route_template(scope)
            duration = round((time.perf_counter() - started) * 1000, 1)
            code = 500 if failed else status[0]
            if code >= 500:
                level = logging.ERROR
            elif duration >= SLOW_MS:
                level = logging.WARNING
            elif scope.get("path") in QUIET_PATHS:
                level = logging.DEBUG
            else:
                level = logging.INFO
            log.log(level, "request", extra={"event": "request", "method": scope.get("method"), "path": path,
                                              "status": code, "duration_ms": duration})
            request_id_var.reset(token)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.scope.get("msme_request_id", "-")
    return JSONResponse({"detail": "Internal server error", "request_id": request_id}, status_code=500,
                        headers={"X-Request-ID": request_id})


def install(app: FastAPI) -> None:
    """Add the middleware (outermost, so it sees everything) and the generic 500 handler."""
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)
