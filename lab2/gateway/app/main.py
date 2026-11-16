"""API Gateway: the only entry point for clients of the bookstore.

* routing: /api/... -> the owning service (explicit routes, so they appear in the gateway's OpenAPI)
* authentication: verifies the JWT once; services receive X-User-Id / X-User-Role
  (client-supplied X-User-* headers are always dropped, so they cannot be forged)
* authorization: customer vs admin routes
* rate limiting: sliding window per user (or per IP for anonymous calls)
* request id: created here, forwarded everywhere, returned to the client
* aggregation: book listings join catalog-service (title, price) with inventory-service (stock);
  if inventory is down the catalog is still served, with stock marked unknown (graceful degradation)
* /api/status collects the readiness of every service
* serves the web storefront (static files)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from utm_common.config import env, env_int
from utm_common.logging import REQUEST_ID_HEADER, log_event, request_id_var
from utm_common.security import decode_token
from utm_common.web import ServiceError, create_app

log = logging.getLogger("gateway")

SERVICES = {
    "auth": env("AUTH_URL", "http://auth-service:8000"),
    "user": env("USER_URL", "http://user-service:8000"),
    "catalog": env("CATALOG_URL", "http://catalog-service:8000"),
    "inventory": env("INVENTORY_URL", "http://inventory-service:8000"),
    "cart": env("CART_URL", "http://cart-service:8000"),
    "order": env("ORDER_URL", "http://order-service:8000"),
    "payment": env("PAYMENT_URL", "http://payment-service:8000"),
    "notification": env("NOTIFICATION_URL", "http://notification-service:8000"),
}
STATIC = Path(__file__).parent / "static"

app = create_app("api-gateway", {}, description="Single entry point of the bookstore: routing, JWT auth, roles, "
                                                "rate limiting, request id, catalog + stock aggregation.")
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
LIMITS = {"auth": env_int("RATE_LIMIT_AUTH_PER_MIN", 10), "checkout": env_int("RATE_LIMIT_CHECKOUT_PER_MIN", 10),
          "default": env_int("RATE_LIMIT_PER_MIN", 240)}


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
        return Caller(int(claims["sub"]), claims.get("role", "customer"), ip)
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
def send(service: str, method: str, path: str, c: Caller, *, json=None, params=None, extra_headers=None,
         timeout: float = 10.0) -> httpx.Response:
    headers = {REQUEST_ID_HEADER: request_id_var.get(), **c.headers(), **(extra_headers or {})}
    try:
        return httpx.request(method, SERVICES[service] + path, json=json, params=params, headers=headers,
                             timeout=timeout)
    except httpx.TimeoutException as exc:
        raise ServiceError(504, "upstream_timeout", f"{service}-service did not answer in time") from exc
    except httpx.HTTPError as exc:
        raise ServiceError(503, "upstream_unavailable", f"{service}-service is unavailable") from exc


def forward(service: str, method: str, path: str, c: Caller, *, limit: str = "default", **kwargs) -> Response:
    limiter.check(f"{limit}:{c.key}", LIMITS[limit])
    resp = send(service, method, path, c, **kwargs)
    passthrough = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "retry-after")}
    return Response(content=resp.content, status_code=resp.status_code, headers=passthrough)


def with_stock(books: list[dict], c: Caller) -> list[dict]:
    """Aggregation: add the stock of each book from inventory-service. If inventory is unavailable,
    the catalog is still served and stock is null ("unknown")."""
    if not books:
        return books
    stock = {}
    try:
        resp = send("inventory", "GET", "/stock", c, params={"ids": ",".join(str(b["id"]) for b in books)},
                    timeout=3)
        if resp.status_code == 200:
            stock = {s["book_id"]: s["available"] for s in resp.json()}
    except ServiceError:
        log_event(log, "Inventory unavailable, serving catalog without stock", logging.WARNING)
    for b in books:
        b["available"] = stock.get(b["id"]) if stock else None
    return books


# ---------- request bodies (documented in the gateway OpenAPI) ----------
class RegisterIn(BaseModel):
    email: str
    password: str = Field(min_length=8)
    full_name: str


class LoginIn(BaseModel):
    email: str
    password: str


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = None
    city: str | None = None
    street: str | None = None


class QuantityIn(BaseModel):
    quantity: int = Field(ge=1, le=10)


class Shipping(BaseModel):
    full_name: str
    phone: str
    city: str
    street: str


class CheckoutIn(BaseModel):
    shipping: Shipping
    payment_method: str = Field(description="card_success | card_decline | cash_on_delivery (simulated)")


class BookIn(BaseModel):
    title: str
    author: str
    genre: str
    year: int
    language: str
    pages: int
    price_cents: int
    description: str = ""
    initial_stock: int = 0


class BookUpdate(BaseModel):
    price_cents: int | None = None
    description: str | None = None
    active: bool | None = None


class StockSet(BaseModel):
    available: int = Field(ge=0)


# ---------- auth & profile ----------
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


# ---------- catalog (aggregated with stock) ----------
@app.get("/api/books", tags=["catalog"], summary="Books with stock (catalog + inventory aggregation)")
def books(q: str | None = None, genre: str | None = None, language: str | None = None, sort: str = "title",
          limit: int = Query(24, ge=1, le=100), offset: int = Query(0, ge=0), c: Caller = Depends(caller)):
    limiter.check(f"default:{c.key}", LIMITS["default"])
    params = {k: v for k, v in {"q": q, "genre": genre, "language": language, "sort": sort,
                                "limit": limit, "offset": offset}.items() if v is not None}
    resp = send("catalog", "GET", "/books", c, params=params)
    if resp.status_code != 200:
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
    page = resp.json()
    page["items"] = with_stock(page["items"], c)
    return page


@app.get("/api/books/{book_id}", tags=["catalog"])
def book(book_id: int, c: Caller = Depends(caller)):
    resp = send("catalog", "GET", f"/books/{book_id}", c)
    if resp.status_code != 200:
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
    return with_stock([resp.json()], c)[0]


@app.get("/api/genres", tags=["catalog"])
def genres(c: Caller = Depends(caller)):
    return forward("catalog", "GET", "/genres", c)


# ---------- cart ----------
@app.get("/api/cart", tags=["cart"])
def cart(c: Caller = Depends(authenticated)):
    return forward("cart", "GET", "/cart", c)


@app.put("/api/cart/items/{book_id}", tags=["cart"], summary="Add a book or set its quantity")
def cart_set(book_id: int, body: QuantityIn, c: Caller = Depends(authenticated)):
    return forward("cart", "PUT", f"/cart/items/{book_id}", c, json=body.model_dump())


@app.delete("/api/cart/items/{book_id}", tags=["cart"])
def cart_remove(book_id: int, c: Caller = Depends(authenticated)):
    return forward("cart", "DELETE", f"/cart/items/{book_id}", c)


# ---------- orders & payments ----------
@app.post("/api/orders", tags=["orders"], summary="Checkout the cart (send Idempotency-Key to avoid double orders)")
def checkout(body: CheckoutIn, c: Caller = Depends(authenticated),
             idempotency_key: str | None = Header(default=None)):
    extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    return forward("order", "POST", "/orders", c, json=body.model_dump(), extra_headers=extra, limit="checkout")


@app.get("/api/orders", tags=["orders"])
def orders(c: Caller = Depends(authenticated)):
    return forward("order", "GET", "/orders", c)


@app.get("/api/orders/{order_id}", tags=["orders"])
def order(order_id: int, c: Caller = Depends(authenticated)):
    return forward("order", "GET", f"/orders/{order_id}", c)


@app.post("/api/orders/{order_id}/cancel", tags=["orders"])
def cancel(order_id: int, c: Caller = Depends(authenticated)):
    return forward("order", "POST", f"/orders/{order_id}/cancel", c)


@app.get("/api/orders/{order_id}/payment", tags=["payments"])
def payment(order_id: int, c: Caller = Depends(authenticated)):
    return forward("payment", "GET", "/payments", c, params={"order_id": order_id})


# ---------- notifications ----------
@app.get("/api/notifications", tags=["notifications"])
def notifications(unread_only: bool = False, c: Caller = Depends(authenticated)):
    return forward("notification", "GET", "/notifications", c, params={"unread_only": unread_only})


@app.post("/api/notifications/{notification_id}/read", tags=["notifications"])
def read_notification(notification_id: int, c: Caller = Depends(authenticated)):
    return forward("notification", "POST", f"/notifications/{notification_id}/read", c)


# ---------- admin ----------
@app.post("/api/admin/books", tags=["admin"])
def admin_create_book(body: BookIn, c: Caller = Depends(admin)):
    return forward("catalog", "POST", "/books", c, json=body.model_dump())


@app.patch("/api/admin/books/{book_id}", tags=["admin"])
def admin_update_book(book_id: int, body: BookUpdate, c: Caller = Depends(admin)):
    return forward("catalog", "PATCH", f"/books/{book_id}", c, json=body.model_dump(exclude_none=True))


@app.put("/api/admin/stock/{book_id}", tags=["admin"], summary="Restock a book")
def admin_stock(book_id: int, body: StockSet, c: Caller = Depends(admin)):
    return forward("inventory", "PUT", f"/stock/{book_id}", c, json=body.model_dump())


@app.get("/api/admin/orders", tags=["admin"])
def admin_orders(status: str | None = None, c: Caller = Depends(admin)):
    return forward("order", "GET", "/admin/orders", c, params={"status": status} if status else None)


@app.get("/api/admin/stats", tags=["admin"])
def admin_stats(c: Caller = Depends(admin)):
    return forward("order", "GET", "/admin/stats", c)


@app.post("/api/admin/orders/{order_id}/ship", tags=["admin"])
def admin_ship(order_id: int, c: Caller = Depends(admin)):
    return forward("order", "POST", f"/admin/orders/{order_id}/ship", c)


@app.post("/api/admin/orders/{order_id}/deliver", tags=["admin"])
def admin_deliver(order_id: int, c: Caller = Depends(admin)):
    return forward("order", "POST", f"/admin/orders/{order_id}/deliver", c)


# ---------- status & storefront ----------
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
        return JSONResponse({"message": "Storefront is not bundled; see /docs"})
    return FileResponse(STATIC / "index.html")
