import json
import logging

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import load_service
from utm_common.logging import JsonFormatter, request_id_var
from utm_common.security import create_token, hash_password, verify_password


@pytest.fixture
def gateway(monkeypatch):
    gw = load_service("gateway")
    calls = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    monkeypatch.setattr(gw.httpx, "request", fake_request)
    return gw, TestClient(gw.app), calls


def test_gateway_forwards_identity_from_token_not_from_client(gateway):
    gw, client, calls = gateway
    token, _ = create_token(42, "a@utm.md", "student")
    r = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}", "X-User-Id": "1",
                                             "X-User-Role": "admin"})
    assert r.status_code == 200
    sent = calls[-1]["headers"]
    assert sent["X-User-Id"] == "42" and sent["X-User-Role"] == "student"  # forged headers ignored
    assert sent["X-Request-ID"] == r.headers["X-Request-ID"]


def test_gateway_requires_auth_and_admin(gateway):
    _, client, calls = gateway
    assert client.get("/api/history").status_code == 401
    student, _ = create_token(1, "s@utm.md", "student")
    assert client.post("/api/ingest/run", headers={"Authorization": f"Bearer {student}"}).status_code == 403
    assert client.get("/api/history", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert calls == []  # nothing reached the services


def test_gateway_rate_limit(gateway, monkeypatch):
    gw, client, _ = gateway
    monkeypatch.setitem(gw.LIMITS, "auth", 2)
    body = {"email": "a@utm.md", "password": "x" * 8}
    codes = [client.post("/api/auth/login", json=body).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_gateway_maps_unreachable_service_to_503(gateway, monkeypatch):
    gw, client, _ = gateway

    def down(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(gw.httpx, "request", down)
    r = client.post("/api/ask", json={"question": "Ce este ECTS?"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream_unavailable"


def test_password_hash_roundtrip():
    stored = hash_password("parola-mea")
    assert verify_password("parola-mea", stored) and not verify_password("alta", stored)
    assert stored != hash_password("parola-mea")  # random salt


def test_json_log_contains_required_fields():
    token = request_id_var.set("abc123")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "Order created", None, None)
    entry = json.loads(JsonFormatter().format(record))
    request_id_var.reset(token)
    assert {"timestamp", "service", "level", "request_id", "message"} <= entry.keys()
    assert entry["request_id"] == "abc123" and entry["level"] == "INFO"


def test_rate_limiter_window():
    from conftest import load_service as _  # noqa: F401
    gw = load_service("gateway")
    limiter = gw.RateLimiter()
    limiter.check("k", 1)
    with pytest.raises(HTTPException) as exc:
        limiter.check("k", 1)
    assert exc.value.status_code == 429
