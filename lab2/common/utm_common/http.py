"""Synchronous service-to-service calls: timeout, a retry on transient failures, request-id forwarding,
and mapping of downstream failures to clear errors (503 / 504) instead of a stack trace."""
from __future__ import annotations

import logging
import time

import httpx

from .logging import REQUEST_ID_HEADER, log_event, request_id_var
from .web import ServiceError

log = logging.getLogger("http.client")

RETRY_STATUSES = {502, 503, 504}


def call(method: str, url: str, *, timeout: float = 10.0, retries: int = 2, headers: dict | None = None,
         **kwargs) -> httpx.Response:
    headers = {**(headers or {}), REQUEST_ID_HEADER: request_id_var.get()}
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.request(method, url, timeout=timeout, headers=headers, **kwargs)
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
            return resp
        except httpx.TimeoutException as exc:
            last = exc
            log_event(log, "Downstream timeout", logging.WARNING, url=url, attempt=attempt + 1)
        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            last = exc
            log_event(log, "Downstream unavailable", logging.WARNING, url=url, attempt=attempt + 1, error=str(exc))
        time.sleep(0.3 * (attempt + 1))
    if isinstance(last, httpx.TimeoutException):
        raise ServiceError(504, "upstream_timeout", f"Dependency did not answer in time: {url.split('/')[2]}")
    raise ServiceError(503, "upstream_unavailable", f"Dependency unavailable: {url.split('/')[2]}")
