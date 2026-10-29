"""HTTP client with retries, exponential backoff and a polite delay."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, user_agent: str, timeout_s: float = 30, max_retries: int = 4, delay_s: float = 0.3):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.delay_s = delay_s

    def get(self, url: str, params: dict | None = None, stream: bool = False) -> requests.Response:
        """GET with retries. Raises the last error when every attempt failed."""
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                time.sleep(self.delay_s)
                resp = self.session.get(url, params=params, timeout=self.timeout_s, stream=stream)
                if resp.status_code in RETRY_STATUSES:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                return resp
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                status = getattr(exc.response, "status_code", None) if isinstance(exc, requests.HTTPError) else None
                # 4xx except 429 will not get better on retry
                if status is not None and status not in RETRY_STATUSES:
                    raise
                last_error = exc
                wait = 2 ** attempt
                log.warning("GET %s failed (%s), attempt %d/%d, retry in %ds", url, exc, attempt, self.max_retries, wait)
                time.sleep(wait)
        assert last_error is not None
        raise last_error
