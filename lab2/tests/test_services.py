from fastapi.testclient import TestClient

from conftest import auth_headers, load_service


# ---------- auth-service ----------
def test_register_login_and_duplicate_email(published):
    svc = load_service("services/auth-service")
    client = TestClient(svc.app)
    body = {"email": "Ana@UTM.md", "password": "secret-pass-1", "full_name": "Ana Popescu", "language": "ro"}

    r = client.post("/auth/register", json=body)
    assert r.status_code == 201
    assert r.json()["token_type"] == "bearer"
    assert published == [("user.registered", {"user_id": 1, "email": "ana@utm.md", "full_name": "Ana Popescu",
                                               "faculty": None, "language": "ro"})]

    dup = client.post("/auth/register", json=body)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"

    ok = client.post("/auth/login", json={"email": "ana@utm.md", "password": "secret-pass-1"})
    assert ok.status_code == 200
    bad = client.post("/auth/login", json={"email": "ana@utm.md", "password": "wrong-pass"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"


def test_register_validation_error_is_structured(published):
    client = TestClient(load_service("services/auth-service").app)
    r = client.post("/auth/register", json={"email": "not-an-email", "password": "short", "full_name": "A"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert {d["field"] for d in err["details"]} >= {"email", "password", "full_name"}
    assert "Traceback" not in r.text


# ---------- user-service ----------
def test_profile_created_from_event_and_idempotent():
    svc = load_service("services/user-service")
    client = TestClient(svc.app)
    event = {"id": "e1", "type": "user.registered",
             "payload": {"user_id": 7, "email": "a@utm.md", "full_name": "Ana", "faculty": "FCIM", "language": "ru"}}
    svc.on_user_registered(event)
    svc.on_user_registered(event)  # redelivery must not fail or duplicate

    me = client.get("/users/me", headers=auth_headers(7))
    assert me.status_code == 200 and me.json()["faculty"] == "FCIM"
    upd = client.patch("/users/me", json={"language": "en"}, headers=auth_headers(7))
    assert upd.json()["language"] == "en"
    assert client.get("/users/7", headers=auth_headers(8)).status_code == 403
    assert client.get("/users/me").status_code == 401


# ---------- history-service ----------
def _answered(event_id, user_id, refused=False):
    return {"id": event_id, "type": "question.answered",
            "payload": {"user_id": user_id, "question": "Câte credite?", "answer": "60 [1]", "refused": refused,
                        "confidence": "high", "latency_s": 4.2, "sources": [{"title": "ECTS", "url": "https://u"}]}}


def test_history_is_per_user_and_deduplicated():
    svc = load_service("services/history-service")
    client = TestClient(svc.app)
    svc.on_question_answered(_answered("a", 1))
    svc.on_question_answered(_answered("a", 1))
    svc.on_question_answered(_answered("b", 1, refused=True))
    svc.on_question_answered(_answered("c", 2))

    page = client.get("/history", headers=auth_headers(1)).json()
    assert page["total"] == 2
    stats = client.get("/history/stats", headers=auth_headers(1)).json()
    assert stats == {"questions": 2, "refused": 1, "avg_latency_s": 4.2}
    other = page["items"][0]["id"]
    assert client.delete(f"/history/{other}", headers=auth_headers(2)).status_code == 404  # not hers
    assert client.delete(f"/history/{other}", headers=auth_headers(1)).status_code == 204


# ---------- notification-service ----------
def test_broadcast_read_state_is_per_user():
    svc = load_service("services/notification-service")
    client = TestClient(svc.app)
    svc.on_event({"id": "d1", "type": "documents.updated", "payload": {"new": 3, "updated": 1, "deleted": 0,
                                                                      "documents": 879}})
    svc.on_event({"id": "q1", "type": "question.answered", "payload": {**_answered("x", 1)["payload"], "refused": False}})

    one = client.get("/notifications", headers=auth_headers(1)).json()
    assert [n["kind"] for n in one] == ["documents_updated"]  # answered questions do not notify
    assert client.post(f"/notifications/{one[0]['id']}/read", headers=auth_headers(1)).status_code == 204
    assert client.get("/notifications?unread_only=true", headers=auth_headers(1)).json() == []
    assert len(client.get("/notifications?unread_only=true", headers=auth_headers(2)).json()) == 1


# ---------- health ----------
def test_health_and_ready_endpoints():
    client = TestClient(load_service("services/history-service").app)
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready")
    assert ready.status_code == 200 and ready.json()["checks"] == ["database"]
