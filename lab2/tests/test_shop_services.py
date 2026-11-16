"""auth, user, catalog, inventory, cart, payment, notification."""
import httpx
from fastapi.testclient import TestClient

from conftest import auth_headers, event, load_service

ADMIN = auth_headers(99, "admin")


# ---------- auth ----------
def test_register_login_duplicate_and_validation(published):
    client = TestClient(load_service("services/auth-service").app)
    body = {"email": "Ana@Mail.md", "password": "secret-pass-1", "full_name": "Ana Popescu"}
    r = client.post("/auth/register", json=body)
    assert r.status_code == 201 and r.json()["role"] == "customer"
    assert published == [("user.registered", {"user_id": 1, "email": "ana@mail.md", "full_name": "Ana Popescu"})]
    assert client.post("/auth/register", json=body).json()["error"]["code"] == "email_taken"
    assert client.post("/auth/login", json={"email": "ana@mail.md", "password": "secret-pass-1"}).status_code == 200
    assert client.post("/auth/login", json={"email": "ana@mail.md", "password": "wrong"}).status_code == 401
    bad = client.post("/auth/register", json={"email": "x", "password": "1", "full_name": ""})
    assert bad.status_code == 422 and {d["field"] for d in bad.json()["error"]["details"]} >= {"email", "password"}
    assert "Traceback" not in bad.text


# ---------- user ----------
def test_profile_from_event_is_idempotent_and_private():
    svc = load_service("services/user-service")
    client = TestClient(svc.app)
    ev = event("user.registered", {"user_id": 7, "email": "a@mail.md", "full_name": "Ana"}, "e1")
    svc.on_user_registered(ev)
    svc.on_user_registered(ev)
    upd = client.patch("/users/me", json={"city": "Chișinău", "street": "Studenților 9", "phone": "+373 69 000000"},
                       headers=auth_headers(7))
    assert upd.status_code == 200 and upd.json()["city"] == "Chișinău"
    assert client.get("/users/7", headers=auth_headers(8)).status_code == 403
    assert client.get("/users/me").status_code == 401


# ---------- catalog ----------
def test_catalog_seed_search_and_admin(published):
    svc = load_service("services/catalog-service")
    svc.seed_catalog()
    client = TestClient(svc.app)
    assert len([e for e in published if e[0] == "book.created"]) == len(svc.BOOKS)

    page = client.get("/books", params={"q": "orwell"}).json()
    assert page["total"] == 1 and page["items"][0]["title"] == "1984"
    assert client.get("/books", params={"genre": "Programming", "sort": "-price"}).json()["items"][0]["price_cents"] == 89900
    assert {g["genre"] for g in client.get("/genres").json()} >= {"Classics", "Programming"}
    assert client.get("/books/999").status_code == 404

    new = {"title": "Test Book", "author": "A", "genre": "Classics", "year": 2020, "language": "en",
           "pages": 10, "price_cents": 1000, "initial_stock": 5}
    assert client.post("/books", json=new, headers=auth_headers(1)).status_code == 403
    created = client.post("/books", json=new, headers=ADMIN)
    assert created.status_code == 201
    assert published[-1] == ("book.created", {"book_id": created.json()["id"], "title": "Test Book",
                                              "initial_stock": 5})


# ---------- inventory ----------
def _inventory(published):
    svc = load_service("services/inventory-service")
    for book_id, qty in ((1, 5), (2, 1)):
        svc.on_book_created(event("book.created", {"book_id": book_id, "initial_stock": qty}))
    return svc, TestClient(svc.app)


def _stock(client):
    return {s["book_id"]: s for s in client.get("/stock", params={"ids": "1,2"}).json()}


def test_reservation_is_all_or_nothing(published):
    svc, client = _inventory(published)
    order = {"order_id": 10, "user_id": 1, "items": [{"book_id": 1, "quantity": 2}, {"book_id": 2, "quantity": 3}]}
    svc.on_order_event(event("order.created", order))
    assert published[-1][0] == "inventory.rejected"
    assert published[-1][1]["short"] == [{"book_id": 2, "requested": 3, "available": 1}]
    assert _stock(client)[1]["available"] == 5  # book 1 was NOT partly reserved


def test_reserve_pay_and_cancel_paths(published):
    svc, client = _inventory(published)
    ok = {"order_id": 11, "user_id": 1, "items": [{"book_id": 1, "quantity": 2}]}
    svc.on_order_event(event("order.created", ok))
    svc.on_order_event(event("order.created", ok, "dup"))  # redelivery reserves nothing more
    assert published[-1] == ("inventory.reserved", {"order_id": 11})
    assert (_stock(client)[1]["available"], _stock(client)[1]["reserved"]) == (3, 2)

    svc.on_order_event(event("order.paid", {"order_id": 11}))
    assert (_stock(client)[1]["reserved"], _stock(client)[1]["sold"]) == (0, 2)

    svc.on_order_event(event("order.cancelled", {"order_id": 11}))  # e.g. cash on delivery cancelled before shipping
    svc.on_order_event(event("order.cancelled", {"order_id": 11}, "dup2"))
    assert (_stock(client)[1]["available"], _stock(client)[1]["sold"]) == (5, 0)


def test_last_copy_goes_to_one_order_only(published):
    svc, client = _inventory(published)
    for oid in (20, 21):
        svc.on_order_event(event("order.created", {"order_id": oid, "user_id": oid,
                                                   "items": [{"book_id": 2, "quantity": 1}]}))
    assert [e[0] for e in published[-2:]] == ["inventory.reserved", "inventory.rejected"]
    assert client.put("/stock/2", json={"available": 4}, headers=auth_headers(1)).status_code == 403
    assert client.put("/stock/2", json={"available": 4}, headers=ADMIN).json()["available"] == 4


# ---------- cart ----------
BOOKS = {1: {"id": 1, "title": "1984", "author": "Orwell", "cover_color": "#000", "price_cents": 16900},
         2: {"id": 2, "title": "Dune", "author": "Herbert", "cover_color": "#111", "price_cents": 25900}}


def test_cart_totals_and_cleanup_after_order(monkeypatch, published):
    svc = load_service("services/cart-service")
    monkeypatch.setattr(svc, "fetch_books", lambda ids: {i: BOOKS[i] for i in ids if i in BOOKS})
    client = TestClient(svc.app)
    client.put("/cart/items/1", json={"quantity": 2}, headers=auth_headers(1))
    cart = client.put("/cart/items/2", json={"quantity": 1}, headers=auth_headers(1)).json()
    assert (cart["total_cents"], cart["count"]) == (2 * 16900 + 25900, 3)
    assert client.put("/cart/items/5", json={"quantity": 1}, headers=auth_headers(1)).status_code == 404
    assert client.put("/cart/items/1", json={"quantity": 50}, headers=auth_headers(1)).status_code == 422
    assert client.get("/cart", headers=auth_headers(2)).json()["items"] == []  # carts are per user

    svc.on_order_created(event("order.created", {"order_id": 1, "user_id": 1, "items": [{"book_id": 1, "quantity": 2}]}))
    assert [i["book_id"] for i in client.get("/cart", headers=auth_headers(1)).json()["items"]] == [2]


def test_cart_reports_catalog_outage_as_503(monkeypatch):
    svc = load_service("services/cart-service")

    def down(*a, **kw):
        return httpx.Response(503)

    monkeypatch.setattr(svc, "call", down)
    r = TestClient(svc.app).put("/cart/items/1", json={"quantity": 1}, headers=auth_headers(1))
    assert r.status_code == 503 and r.json()["error"]["code"] == "catalog_unavailable"


# ---------- payment ----------
def test_payment_methods_and_no_double_charge(published):
    svc = load_service("services/payment-service")
    client = TestClient(svc.app)
    svc.on_payment_requested(event("payment.requested", {"order_id": 1, "user_id": 1, "amount_cents": 500,
                                                         "method": "card_success"}))
    svc.on_payment_requested(event("payment.requested", {"order_id": 2, "user_id": 1, "amount_cents": 500,
                                                         "method": "card_decline"}))
    svc.on_payment_requested(event("payment.requested", {"order_id": 1, "user_id": 1, "amount_cents": 500,
                                                         "method": "card_success"}, "redelivery"))
    kinds = [e[0] for e in published]
    assert kinds == ["payment.succeeded", "payment.failed", "payment.succeeded"]
    assert published[0][1]["payment_id"] == published[2][1]["payment_id"]  # same payment, not a second charge
    assert client.get("/payments", params={"order_id": 1}, headers=auth_headers(1)).json()["status"] == "succeeded"
    assert client.get("/payments", params={"order_id": 1}, headers=auth_headers(2)).status_code == 404


# ---------- notification ----------
def test_order_notifications_are_per_user():
    svc = load_service("services/notification-service")
    client = TestClient(svc.app)
    svc.on_event(event("order.created", {"order_id": 5, "user_id": 1, "total_cents": 16900}))
    svc.on_event(event("order.cancelled", {"order_id": 5, "user_id": 1, "reason_text": "Платёж отклонён"}))
    svc.on_event(event("order.cancelled", {"order_id": 5, "user_id": 1}))  # same event id: ignored
    mine = client.get("/notifications", headers=auth_headers(1)).json()
    assert [n["title"] for n in mine] == ["Заказ №5 отменён", "Заказ №5 принят"] or \
           sorted(n["title"] for n in mine) == ["Заказ №5 отменён", "Заказ №5 принят"]
    assert "169.00 MDL" in next(n["body"] for n in mine if n["kind"] == "order_created")
    assert client.get("/notifications", headers=auth_headers(2)).json() == []
    nid = mine[0]["id"]
    assert client.post(f"/notifications/{nid}/read", headers=auth_headers(1)).status_code == 204
    assert len(client.get("/notifications?unread_only=true", headers=auth_headers(1)).json()) == 1
