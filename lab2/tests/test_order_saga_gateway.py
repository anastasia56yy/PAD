"""order-service saga, gateway (identity, roles, rate limit, aggregation), shared library."""
import json
import logging

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import auth_headers, event, load_service
from utm_common.logging import JsonFormatter, request_id_var
from utm_common.security import create_token, hash_password, verify_password

CART = {"items": [{"book_id": 1, "title": "1984", "unit_price_cents": 16900, "quantity": 2},
                  {"book_id": 2, "title": "Dune", "unit_price_cents": 25900, "quantity": 1}],
        "total_cents": 59700, "count": 3}
CHECKOUT = {"shipping": {"full_name": "Ana Popescu", "phone": "+373 69 123456", "city": "Chișinău",
                         "street": "bd. Ștefan cel Mare 168"}, "payment_method": "card_success"}


@pytest.fixture
def orders(monkeypatch, published):
    svc = load_service("services/order-service")
    monkeypatch.setattr(svc, "call", lambda *a, **kw: httpx.Response(200, json=CART))
    return svc, TestClient(svc.app), published


def _checkout(client, method="card_success", key=None, user=1):
    headers = {**auth_headers(user), **({"Idempotency-Key": key} if key else {})}
    return client.post("/orders", json={**CHECKOUT, "payment_method": method}, headers=headers)


def _status(client, oid, user=1):
    return client.get(f"/orders/{oid}", headers=auth_headers(user)).json()["status"]


def test_happy_path_card(orders):
    svc, client, published = orders
    r = _checkout(client)
    assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["total_cents"] == 59700
    oid = r.json()["id"]
    assert published[-1] == ("order.created", {"order_id": oid, "user_id": 1, "total_cents": 59700,
                                               "items": [{"book_id": 1, "quantity": 2}, {"book_id": 2, "quantity": 1}]})
    svc.on_saga_event(event("inventory.reserved", {"order_id": oid}))
    assert _status(client, oid) == "awaiting_payment"
    assert published[-1] == ("payment.requested", {"order_id": oid, "user_id": 1, "amount_cents": 59700,
                                                   "method": "card_success"})
    svc.on_saga_event(event("payment.succeeded", {"order_id": oid, "payment_id": "p1", "method": "card_success"}))
    assert _status(client, oid) == "paid" and published[-1][0] == "order.paid"
    # a late/duplicate event does not move the order back
    svc.on_saga_event(event("inventory.reserved", {"order_id": oid}, "late"))
    assert _status(client, oid) == "paid"


def test_out_of_stock_cancels(orders):
    svc, client, published = orders
    oid = _checkout(client).json()["id"]
    svc.on_saga_event(event("inventory.rejected", {"order_id": oid, "short": [{"book_id": 2}]}))
    order = client.get(f"/orders/{oid}", headers=auth_headers(1)).json()
    assert order["status"] == "cancelled" and order["cancel_reason"] == "out_of_stock"
    assert published[-1][0] == "order.cancelled" and published[-1][1]["reason"] == "out_of_stock"


def test_declined_card_cancels_and_releases_stock(orders):
    svc, client, published = orders
    oid = _checkout(client, "card_decline").json()["id"]
    svc.on_saga_event(event("inventory.reserved", {"order_id": oid}))
    svc.on_saga_event(event("payment.failed", {"order_id": oid, "reason": "declined"}))
    assert _status(client, oid) == "cancelled"
    assert published[-1] == ("order.cancelled", {"order_id": oid, "user_id": 1, "reason": "payment_declined",
                                                 "reason_text": "Платёж отклонён"})


def test_cash_on_delivery_cancel_and_admin_flow(orders):
    svc, client, published = orders
    oid = _checkout(client, "cash_on_delivery").json()["id"]
    svc.on_saga_event(event("inventory.reserved", {"order_id": oid}))
    svc.on_saga_event(event("payment.succeeded", {"order_id": oid, "method": "cash_on_delivery"}))
    assert _status(client, oid) == "confirmed"
    assert client.post(f"/orders/{oid}/cancel", headers=auth_headers(2)).status_code == 404  # not her order
    assert client.post(f"/orders/{oid}/cancel", headers=auth_headers(1)).json()["status"] == "cancelled"

    paid = _checkout(client).json()["id"]
    svc.on_saga_event(event("inventory.reserved", {"order_id": paid}))
    svc.on_saga_event(event("payment.succeeded", {"order_id": paid, "method": "card_success"}))
    assert client.post(f"/orders/{paid}/cancel", headers=auth_headers(1)).status_code == 409
    admin = auth_headers(99, "admin")
    assert client.post(f"/admin/orders/{paid}/deliver", headers=admin).status_code == 409  # must ship first
    assert client.post(f"/admin/orders/{paid}/ship", headers=auth_headers(1)).status_code == 403
    assert client.post(f"/admin/orders/{paid}/ship", headers=admin).json()["status"] == "shipped"
    assert client.post(f"/admin/orders/{paid}/deliver", headers=admin).json()["status"] == "delivered"
    stats = client.get("/admin/stats", headers=admin).json()
    assert stats["revenue_cents"] == 59700 and stats["orders_by_status"]["cancelled"] == 1


def test_idempotency_key_and_empty_cart(orders, monkeypatch):
    svc, client, published = orders
    first = _checkout(client, key="click-1").json()
    second = _checkout(client, key="click-1").json()
    assert first["id"] == second["id"] and len([e for e in published if e[0] == "order.created"]) == 1
    monkeypatch.setattr(svc, "call", lambda *a, **kw: httpx.Response(200, json={"items": [], "total_cents": 0}))
    r = _checkout(client)
    assert r.status_code == 400 and r.json()["error"]["code"] == "empty_cart"
    bad = client.post("/orders", json={**CHECKOUT, "payment_method": "bitcoin"}, headers=auth_headers(1))
    assert bad.status_code == 422


# ---------- gateway ----------
@pytest.fixture
def gateway(monkeypatch):
    gw = load_service("gateway")
    calls = []

    def fake(method, url, json=None, params=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "params": params, "headers": headers})
        if url.endswith("/books"):
            return httpx.Response(200, json={"total": 2, "items": [{"id": 1, "title": "1984"},
                                                                   {"id": 2, "title": "Dune"}]})
        if url.endswith("/stock"):
            return httpx.Response(200, json=[{"book_id": 1, "available": 3}])
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    monkeypatch.setattr(gw.httpx, "request", fake)
    return gw, TestClient(gw.app), calls


def test_gateway_aggregates_catalog_and_stock(gateway, monkeypatch):
    gw, client, _ = gateway
    items = client.get("/api/books").json()["items"]
    assert [(b["id"], b["available"]) for b in items] == [(1, 3), (2, None)]

    def inventory_down(method, url, **kw):
        if url.endswith("/stock"):
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"total": 1, "items": [{"id": 1, "title": "1984"}]})

    monkeypatch.setattr(gw.httpx, "request", inventory_down)
    degraded = client.get("/api/books")
    assert degraded.status_code == 200 and degraded.json()["items"][0]["available"] is None


def test_gateway_identity_roles_and_errors(gateway, monkeypatch):
    gw, client, calls = gateway
    token, _ = create_token(42, "a@mail.md", "customer")
    r = client.get("/api/cart", headers={"Authorization": f"Bearer {token}", "X-User-Id": "1", "X-User-Role": "admin"})
    assert r.status_code == 200
    sent = calls[-1]["headers"]
    assert sent["X-User-Id"] == "42" and sent["X-User-Role"] == "customer"  # forged headers ignored
    assert sent["X-Request-ID"] == r.headers["X-Request-ID"]

    assert client.get("/api/orders").status_code == 401
    assert client.get("/api/admin/orders", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert client.get("/api/cart", headers={"Authorization": "Bearer garbage"}).status_code == 401

    def down(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(gw.httpx, "request", down)
    r = client.get("/api/cart", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream_unavailable"


def test_gateway_rate_limit_and_idempotency_header(gateway, monkeypatch):
    gw, client, calls = gateway
    monkeypatch.setitem(gw.LIMITS, "auth", 2)
    codes = [client.post("/api/auth/login", json={"email": "a@b.md", "password": "x"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    token, _ = create_token(1, "a@mail.md", "customer")
    client.post("/api/orders", json=CHECKOUT, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"})
    assert calls[-1]["headers"]["Idempotency-Key"] == "k1"


# ---------- shared library ----------
def test_password_hash_and_json_logs():
    stored = hash_password("parola-mea")
    assert verify_password("parola-mea", stored) and not verify_password("alta", stored)
    token = request_id_var.set("abc123")
    entry = json.loads(JsonFormatter().format(logging.LogRecord("x", logging.INFO, __file__, 1, "Order created",
                                                                None, None)))
    request_id_var.reset(token)
    assert {"timestamp", "service", "level", "request_id", "message"} <= entry.keys()
    assert entry["request_id"] == "abc123"


def test_rate_limiter_window():
    limiter = load_service("gateway").RateLimiter()
    limiter.check("k", 1)
    with pytest.raises(HTTPException) as exc:
        limiter.check("k", 1)
    assert exc.value.status_code == 429


def test_health_and_ready():
    client = TestClient(load_service("services/order-service").app)
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["checks"] == ["database"]


# ---------- transactional outbox ----------
def test_outbox_sends_only_committed_events_in_order_and_survives_broker_outage(monkeypatch):
    svc = load_service("services/order-service")
    from utm_common import events, outbox

    with svc.Session() as s:
        outbox.add(s, "order.created", {"order_id": 1})
        s.rollback()  # the business transaction failed: its event must never be sent
    with svc.Session() as s:
        outbox.add(s, "order.created", {"order_id": 2})
        outbox.add(s, "order.paid", {"order_id": 2})
        s.commit()

    def broker_down(event_type, body):
        raise ConnectionError("broker down")

    monkeypatch.setattr(events, "send_raw", broker_down)
    with pytest.raises(ConnectionError):
        outbox.relay_once(svc.Session)  # nothing lost: rows stay unsent

    sent = []
    monkeypatch.setattr(events, "send_raw", lambda event_type, body: sent.append((event_type, json.loads(body))))
    assert outbox.relay_once(svc.Session) == 2
    assert [(t, b["payload"]["order_id"]) for t, b in sent] == [("order.created", 2), ("order.paid", 2)]
    assert outbox.relay_once(svc.Session) == 0  # already marked as sent
