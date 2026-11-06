"""API Gateway: the only entry point for clients.

* routing: /api/... -> the owning service (explicit routes, so they appear in the gateway's OpenAPI)
* authentication: verifies the JWT once; services receive X-User-Id / X-User-Role
  (client-supplied X-User-* headers are always dropped, so they cannot be forged)
* authorization: admin-only routes
* rate limiting: sliding window per user (or per IP for anonymous calls)
* request id: created here, forwarded everywhere, returned to the client
* aggregation: /api/status collects the readiness of every service
* serves the web interface (static files)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from utm_common.config import env, env_int
from utm_common.logging import REQUEST_ID_HEADER, request_id_var
from utm_common.security import decode_token
from utm_common.web import ServiceError, create_app

log = logging.getLogger("gateway")

SERVICES = {
    "auth": env("AUTH_URL", "http://auth-service:8000"),
    "user": env("USER_URL", "http://user-service:8000"),
    "answer": env("ANSWER_URL", "http://answer-service:8000"),
    "search": env("SEARCH_URL", "http://search-service:8000"),
    "history": env("HISTORY_URL", "http://history-service:8000"),
    "ingestion": env("INGESTION_URL", "http://ingestion-service:8000"),
    "notification": env("NOTIFICATION_URL", "http://notification-service:8000"),
}
STATIC = Path(__file__).parent / "static"

app = create_app("api-gateway", {}, description="Single entry point: routing, JWT auth, rate limiting, request id.")
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- rate limiting ----------
class RateLimiter:
    """Sliding window in memory. With several gateway replicas each keeps its own window
    (a shared store such as Redis would make the limit global)."""

    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: int = 60) -> None:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                retry = int(window_s - (now - q[0])) + 1
                raise HTTPException(status_code=429, detail=f"Too many requests, retry in {retry} s",
                                    headers={"Retry-After": str(retry)})
            q.append(now)


limiter = RateLimiter()
LIMITS = {"ask": env_int("RATE_LIMIT_ASK_PER_MIN", 20), "auth": env_int("RATE_LIMIT_AUTH_PER_MIN", 10),
          "default": env_int("RATE_LIMIT_PER_MIN", 120)}


# ---------- identity ----------
class Caller:
    def __init__(self, user_id: int | None, role: str | None, ip: str):
        self.user_id, self.role, self.ip = user_id, role, ip

    @property
    def key(self) -> str:
        return f"user:{self.user_id}" if self.user_id else f"ip:{self.ip}"

    def headers(self) -> dict:
        return {"X-User-Id": str(self.user_id), "X-User-Role": self.role} if self.user_id else {}


def caller(request: Request) -> Caller:
    auth = request.headers.get("authorization", "")
    ip = request.client.host if request.client else "unknown"
    if auth.lower().startswith("bearer "):
        claims = decode_token(auth[7:].strip())
        return Caller(int(claims["sub"]), claims.get("role", "student"), ip)
    return Caller(None, None, ip)


def authenticated(c: Caller = Depends(caller)) -> Caller:
    if not c.user_id:
        raise HTTPException(status_code=401, detail="Sign in required")
    return c


def admin(c: Caller = Depends(authenticated)) -> Caller:
    if c.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return c


# ---------- proxy ----------
def forward(service: str, method: str, path: str, c: Caller, *, json=None, params=None,
            timeout: float = 15.0, limit: str = "default") -> Response:
    limiter.check(f"{limit}:{c.key}", LIMITS[limit])
    headers = {REQUEST_ID_HEADER: request_id_var.get(), **c.headers()}
    try:
        resp = httpx.request(method, SERVICES[service] + path, json=json, params=params, headers=headers,
                             timeout=timeout)
    except httpx.TimeoutException as exc:
        raise ServiceError(504, "upstream_timeout", f"{service}-service did not answer in time") from exc
    except httpx.HTTPError as exc:
        raise ServiceError(503, "upstream_unavailable", f"{service}-service is unavailable") from exc
    passthrough = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "retry-after")}
    return Response(content=resp.content, status_code=resp.status_code, headers=passthrough)


# ---------- request bodies (documented in the gateway OpenAPI) ----------
class RegisterIn(BaseModel):
    email: str
    password: str = Field(min_length=8)
    full_name: str
    faculty: str | None = None
    language: str = "ro"


class LoginIn(BaseModel):
    email: str
    password: str


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    faculty: str | None = None
    language: str | None = None


# ---------- routes ----------
@app.post("/api/auth/register", tags=["auth"])
def register(body: RegisterIn, c: Caller = Depends(caller)):
    return forward("auth", "POST", "/auth/register", c, json=body.model_dump(), limit="auth")


@app.post("/api/auth/login", tags=["auth"])
def login(body: LoginIn, c: Caller = Depends(caller)):
    return forward("auth", "POST", "/auth/login", c, json=body.model_dump(), limit="auth")


@app.get("/api/auth/me", tags=["auth"])
def me(c: Caller = Depends(authenticated)):
    return forward("auth", "GET", "/auth/me", c)


@app.get("/api/users/me", tags=["users"])
def profile(c: Caller = Depends(authenticated)):
    return forward("user", "GET", "/users/me", c)


@app.patch("/api/users/me", tags=["users"])
def update_profile(body: ProfileUpdate, c: Caller = Depends(authenticated)):
    return forward("user", "PATCH", "/users/me", c, json=body.model_dump(exclude_none=True))


@app.post("/api/ask", tags=["answer"], summary="Ask a question (anonymous allowed; signed-in users get history)")
def ask(body: AskIn, c: Caller = Depends(caller)):
    return forward("answer", "POST", "/ask", c, json=body.model_dump(), timeout=180, limit="ask")


@app.get("/api/info", tags=["search"])
def info(c: Caller = Depends(caller)):
    return forward("search", "GET", "/info", c)


@app.get("/api/history", tags=["history"])
def history(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), c: Caller = Depends(authenticated)):
    return forward("history", "GET", "/history", c, params={"limit": limit, "offset": offset})


@app.get("/api/history/stats", tags=["history"])
def history_stats(c: Caller = Depends(authenticated)):
    return forward("history", "GET", "/history/stats", c)


@app.delete("/api/history/{entry_id}", tags=["history"])
def delete_history(entry_id: int, c: Caller = Depends(authenticated)):
    return forward("history", "DELETE", f"/history/{entry_id}", c)


@app.get("/api/notifications", tags=["notifications"])
def notifications(unread_only: bool = False, c: Caller = Depends(authenticated)):
    return forward("notification", "GET", "/notifications", c, params={"unread_only": unread_only})


@app.post("/api/notifications/{notification_id}/read", tags=["notifications"])
def read_notification(notification_id: int, c: Caller = Depends(authenticated)):
    return forward("notification", "POST", f"/notifications/{notification_id}/read", c)


@app.post("/api/ingest/run", tags=["ingestion"], summary="Start document collection (admin only)")
def ingest_run(c: Caller = Depends(admin)):
    return forward("ingestion", "POST", "/ingest/run", c)


@app.get("/api/ingest/status", tags=["ingestion"])
def ingest_status(c: Caller = Depends(admin)):
    return forward("ingestion", "GET", "/ingest/status", c)


@app.get("/api/status", tags=["health"], summary="Readiness of every service (aggregation)")
def services_status() -> dict:
    result = {}
    for name, url in SERVICES.items():
        try:
            r = httpx.get(url + "/ready", timeout=3)
            result[name] = "ready" if r.status_code == 200 else "not_ready"
        except httpx.HTTPError:
            result[name] = "down"
    return {"gateway": "ready", "services": result}


@app.get("/", include_in_schema=False)
def index():
    if not (STATIC / "index.html").exists():
        raise HTTPException(status_code=404, detail="Web interface is not bundled")
    return FileResponse(STATIC / "index.html")
