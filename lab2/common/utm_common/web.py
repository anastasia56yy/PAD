"""FastAPI app factory used by every service: request-id middleware, access log,
uniform JSON errors without stack traces, /health and /ready."""
from __future__ import annotations

import logging
import time
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import SERVICE_NAME
from .logging import REQUEST_ID_HEADER, log_event, new_request_id, request_id_var, setup_logging

log = logging.getLogger("http")


class ServiceError(Exception):
    """Expected error with an HTTP status and a stable machine-readable code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error_body(code: str, message: str, details=None) -> dict:
    body = {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}
    if details is not None:
        body["error"]["details"] = details
    return body


def create_app(title: str, readiness_checks: dict[str, Callable[[], None]] | None = None,
               description: str = "") -> FastAPI:
    """readiness_checks: name -> callable that raises when the dependency is not usable."""
    setup_logging()
    app = FastAPI(title=title, description=description, version="1.0.0")
    checks = readiness_checks if readiness_checks is not None else {}
    app.state.readiness_checks = checks

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        if request.url.path not in ("/health", "/ready"):
            token = request_id_var.set(rid)
            log_event(log, f"{request.method} {request.url.path}", status=response.status_code,
                      duration_ms=round((time.perf_counter() - start) * 1000, 1))
            request_id_var.reset(token)
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message))

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(exc.status_code, "error")
        return JSONResponse(status_code=exc.status_code, content=error_body(code, str(exc.detail)),
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content=error_body("validation_error", "Invalid input", details))

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error")  # traceback goes to the log only
        return JSONResponse(status_code=500, content=error_body("internal_error", "Internal server error"))

    @app.get("/health", tags=["health"], summary="Liveness: the process is up")
    def health() -> dict:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/ready", tags=["health"], summary="Readiness: dependencies are reachable")
    def ready():
        failed = {}
        for name, check in app.state.readiness_checks.items():
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
                failed[name] = str(exc)[:200]
        if failed:
            return JSONResponse(status_code=503, content={"status": "not_ready", "service": SERVICE_NAME,
                                                          "failed": failed})
        return {"status": "ready", "service": SERVICE_NAME, "checks": list(app.state.readiness_checks)}

    return app
