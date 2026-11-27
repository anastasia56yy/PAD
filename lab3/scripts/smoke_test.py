"""End-to-end check of a deployed Libraria (used by the CD job after deploying to a kind cluster).

    python lab3/scripts/smoke_test.py http://localhost:8090

Only the Python standard library is used, so it runs anywhere. Steps:
  1. every service is ready (/api/status), the catalog has books with stock
  2. a new customer registers and logs in; a request without a token gets 401
  3. the customer buys one book with the simulated card that succeeds
  4. the saga finishes: the order becomes "paid", stock goes down by 1, a notification arrives
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8090").rstrip("/")


def call(method: str, path: str, body: dict | None = None, token: str | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


def step(text: str) -> None:
    print(f"==> {text}", flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAILED: {message}", flush=True)
        sys.exit(1)
    print(f"    ok: {message}", flush=True)


def wait_until(what: str, fn, timeout: float = 180, every: float = 3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
        time.sleep(every)
    print(f"FAILED: timed out waiting for {what} (last: {last})", flush=True)
    sys.exit(1)


step(f"waiting until every service is ready at {BASE}")
def all_ready():
    code, s = call("GET", "/api/status")
    return code == 200 and s.get("gateway") == "ready" and s["services"] and \
        all(v == "ready" for v in s["services"].values()) and s
status = wait_until("/api/status", all_ready)
check(True, f"{len(status['services'])} services ready: {', '.join(status['services'])}")

step("catalog")
books = wait_until("seeded books with stock",
                   lambda: (lambda r: r[0] == 200 and r[1].get("total", 0) > 0 and
                            any(b.get("available", 0) > 0 for b in r[1]["items"]) and r[1])(call("GET", "/api/books")))
book = next(b for b in books["items"] if b.get("available", 0) > 0)
check(books["total"] >= 1, f"{books['total']} books; buying '{book['title']}' (in stock: {book['available']})")

step("security")
code, _ = call("GET", "/api/orders")
check(code == 401, f"GET /api/orders without a token -> {code}")

step("register and log in")
email = f"ci-{uuid.uuid4().hex[:10]}@example.com"
code, _ = call("POST", "/api/auth/register", {"email": email, "password": "ci-password-123", "full_name": "CI Customer"})
check(code in (200, 201), f"register {email} -> {code}")
code, tok = call("POST", "/api/auth/login", {"email": email, "password": "ci-password-123"})
check(code == 200 and tok.get("role") == "customer", f"login -> {code}, role {tok.get('role')}")
token = tok["access_token"]

step("cart and checkout (simulated card that succeeds)")
code, cart = call("PUT", f"/api/cart/items/{book['id']}", {"quantity": 1}, token)
check(code == 200 and cart.get("count") == 1, f"cart has {cart.get('count')} item")
code, order = call("POST", "/api/orders", {
    "shipping": {"full_name": "CI Customer", "phone": "+373 600 00000", "city": "Chisinau", "street": "Stefan cel Mare 1"},
    "payment_method": "card_success",
}, token, {"Idempotency-Key": uuid.uuid4().hex})
check(code in (200, 201, 202) and "id" in order, f"order #{order.get('id')} created, status {order.get('status')}")

step("saga: order -> inventory -> payment")
final = wait_until("the order to become paid",
                   lambda: (lambda r: r[1].get("status") in ("paid", "cancelled") and r[1])(
                       call("GET", f"/api/orders/{order['id']}", token=token)), timeout=90, every=2)
check(final["status"] == "paid", f"order #{order['id']} is {final['status']}")

code, after = call("GET", f"/api/books/{book['id']}")
check(after.get("available") == book["available"] - 1,
      f"stock of '{book['title']}': {book['available']} -> {after.get('available')}")

notes = wait_until("a notification", lambda: (lambda r: r[0] == 200 and r[1])(
    call("GET", "/api/notifications", token=token)), timeout=60, every=2)
count = len(notes) if isinstance(notes, list) else len(notes.get("items", []))
check(count >= 1, f"{count} notification(s) for the customer")

print("\nSMOKE TEST PASSED", flush=True)
