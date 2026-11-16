"""order-service: orders and the checkout saga.

Owns: order_db.
Calls: cart-service (REST) to read the cart at checkout.
Publishes: order.created, payment.requested, order.paid, order.cancelled, order.shipped, order.delivered
Consumes: inventory.reserved / inventory.rejected, payment.succeeded / payment.failed

The checkout is a choreographed saga: no service calls the next one, each reacts to the previous event.
order-service keeps the state machine; a transition only happens from the expected state, so duplicate
or late events are ignored (idempotency) and a failed step is compensated (stock is released).

  pending --inventory.reserved--> awaiting_payment --payment.succeeded--> paid | confirmed (cash on delivery)
     |                                   |
     +--inventory.rejected--> cancelled  +--payment.failed--> cancelled (+ order.cancelled releases the stock)
  paid | confirmed --admin--> shipped --admin--> delivered
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from utm_common import events, outbox
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.http import call
from utm_common.logging import log_event
from utm_common.security import Identity, admin_identity, current_identity
from utm_common.web import ServiceError, create_app

log = logging.getLogger("orders")
CART_URL = env("CART_URL", "http://cart-service:8000")
PAYMENT_METHODS = ("card_success", "card_decline", "cash_on_delivery")
CANCEL_REASONS = {
    "out_of_stock": "Нет нужного количества на складе",
    "payment_declined": "Платёж отклонён",
    "cancelled_by_customer": "Отменён покупателем",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    total_cents: Mapped[int] = mapped_column(Integer)
    payment_method: Mapped[str] = mapped_column(String(20))
    shipping: Mapped[dict] = mapped_column(JSON)
    cancel_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", lazy="selectin",
                                                    cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    book_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))  # copied at checkout: the order keeps the price it was sold at
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    order: Mapped[Order] = relationship(back_populates="items")


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("order-service", {"database": lambda: ping(engine)},
                 description="Orders and the checkout saga (inventory -> payment).")


# ---------- state machine ----------
def transition(order_id: int, expected: tuple[str, ...], new_status: str, emit=None, **fields) -> Order | None:
    """Move the order to new_status only from an expected state; `emit(order)` returns the events of this step,
    which are written to the outbox in the same transaction. Returns None when the event is stale."""
    with Session() as s:
        order = s.get(Order, order_id)
        if order is None or order.status not in expected:
            log_event(log, "Event ignored for order state", order_id=order_id,
                      state=order.status if order else None, wanted=new_status)
            return None
        order.status = new_status
        for k, v in fields.items():
            setattr(order, k, v)
        for event_type, payload in (emit(order) if emit else []):
            outbox.add(s, event_type, payload)
        s.commit()
        s.refresh(order)
    log_event(log, "Order status changed", order_id=order_id, status=new_status)
    return order


def cancelled(reason: str):
    def emit(order: Order):
        return [("order.cancelled", {"order_id": order.id, "user_id": order.user_id, "reason": reason,
                                     "reason_text": CANCEL_REASONS.get(reason, reason)})]
    return emit


def on_saga_event(event: dict) -> None:
    p, kind = event["payload"], event["type"]
    oid = p["order_id"]
    if kind == "inventory.reserved":
        transition(oid, ("pending",), "awaiting_payment", emit=lambda o: [
            ("payment.requested", {"order_id": o.id, "user_id": o.user_id, "amount_cents": o.total_cents,
                                   "method": o.payment_method})])
    elif kind == "inventory.rejected":
        transition(oid, ("pending",), "cancelled", emit=cancelled("out_of_stock"), cancel_reason="out_of_stock",
                   details={"short": p.get("short", [])})
    elif kind == "payment.succeeded":
        new_status = "confirmed" if p.get("method") == "cash_on_delivery" else "paid"
        transition(oid, ("awaiting_payment",), new_status, details={"payment_id": p.get("payment_id")},
                   emit=lambda o: [("order.paid", {"order_id": o.id, "user_id": o.user_id, "status": new_status,
                                                   "total_cents": o.total_cents,
                                                   "items": [{"book_id": i.book_id, "quantity": i.quantity}
                                                             for i in o.items]})])
    elif kind == "payment.failed":
        # compensation: order.cancelled makes inventory release the reserved copies
        transition(oid, ("awaiting_payment",), "cancelled", emit=cancelled("payment_declined"),
                   cancel_reason="payment_declined", details={"payment_error": p.get("reason")})


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("order-service.saga", on_saga_event)
        outbox.start_relay(Session)


# ---------- API models ----------
class Shipping(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(pattern=r"^\+?[0-9 ()-]{6,20}$")
    city: str = Field(min_length=2, max_length=80)
    street: str = Field(min_length=3, max_length=200)


class CheckoutIn(BaseModel):
    shipping: Shipping
    payment_method: str = Field(pattern="^(card_success|card_decline|cash_on_delivery)$",
                                description="Payments are simulated: test card that succeeds, test card that is "
                                            "declined, or cash on delivery")


class ItemOut(BaseModel):
    book_id: int
    title: str
    unit_price_cents: int
    quantity: int


class OrderOut(BaseModel):
    id: int
    user_id: int
    status: str
    total_cents: int
    payment_method: str
    shipping: dict
    cancel_reason: str | None
    cancel_reason_text: str | None = None
    details: dict | None
    created_at: datetime
    updated_at: datetime
    items: list[ItemOut]


def out(order: Order) -> OrderOut:
    o = OrderOut.model_validate(order, from_attributes=True)
    o.cancel_reason_text = CANCEL_REASONS.get(order.cancel_reason) if order.cancel_reason else None
    return o


def owned(order_id: int, identity: Identity) -> Order:
    with Session() as s:
        order = s.get(Order, order_id)
    if order is None or (order.user_id != identity.user_id and identity.role != "admin"):
        raise ServiceError(404, "not_found", f"Order {order_id} not found")
    return order


# ---------- API ----------
@app.post("/orders", response_model=OrderOut, status_code=201, tags=["orders"], summary="Checkout the cart")
def checkout(body: CheckoutIn, identity: Identity = Depends(current_identity),
             idempotency_key: str | None = Header(default=None, max_length=64)) -> OrderOut:
    if idempotency_key:  # the same submit repeated (double click, network retry) returns the first order
        with Session() as s:
            existing = s.scalar(select(Order).where(Order.user_id == identity.user_id,
                                                    Order.idempotency_key == idempotency_key))
        if existing:
            return out(existing)

    resp = call("GET", f"{CART_URL}/cart", headers={"X-User-Id": str(identity.user_id), "X-User-Role": identity.role},
                timeout=5)
    if resp.status_code != 200:
        raise ServiceError(503, "cart_unavailable", "Cart is unavailable, try again")
    cart = resp.json()
    if not cart["items"]:
        raise ServiceError(400, "empty_cart", "The cart is empty")

    with Session() as s:
        order = Order(user_id=identity.user_id, total_cents=cart["total_cents"], payment_method=body.payment_method,
                      shipping=body.shipping.model_dump(), idempotency_key=idempotency_key,
                      items=[OrderItem(book_id=i["book_id"], title=i["title"], unit_price_cents=i["unit_price_cents"],
                                       quantity=i["quantity"]) for i in cart["items"]])
        s.add(order)
        s.flush()
        outbox.add(s, "order.created", {  # starts the saga; saved together with the order
            "order_id": order.id, "user_id": order.user_id, "total_cents": order.total_cents,
            "items": [{"book_id": i.book_id, "quantity": i.quantity} for i in order.items],
        })
        s.commit()
        s.refresh(order)
    log_event(log, "Order created", order_id=order.id, total_cents=order.total_cents, lines=len(order.items))
    return out(order)


@app.get("/orders", response_model=list[OrderOut], tags=["orders"], summary="My orders, newest first")
def my_orders(identity: Identity = Depends(current_identity)) -> list[OrderOut]:
    with Session() as s:
        rows = s.scalars(select(Order).where(Order.user_id == identity.user_id)
                         .order_by(Order.created_at.desc()).limit(50)).all()
    return [out(o) for o in rows]


@app.get("/orders/{order_id}", response_model=OrderOut, tags=["orders"])
def get_order(order_id: int, identity: Identity = Depends(current_identity)) -> OrderOut:
    return out(owned(order_id, identity))


@app.post("/orders/{order_id}/cancel", response_model=OrderOut, tags=["orders"])
def cancel_order(order_id: int, identity: Identity = Depends(current_identity)) -> OrderOut:
    order = owned(order_id, identity)
    # a card payment cannot be undone here (no refunds in the simulated provider): only unpaid orders
    updated = transition(order.id, ("pending", "confirmed"), "cancelled", emit=cancelled("cancelled_by_customer"),
                         cancel_reason="cancelled_by_customer")
    if updated is None:
        raise ServiceError(409, "cannot_cancel", f"An order in status '{order.status}' can no longer be cancelled")
    return out(updated)


@app.get("/admin/orders", response_model=list[OrderOut], tags=["admin"])
def all_orders(status: str | None = Query(None, max_length=20), _: Identity = Depends(admin_identity)):
    stmt = select(Order).order_by(Order.created_at.desc()).limit(100)
    if status:
        stmt = stmt.where(Order.status == status)
    with Session() as s:
        rows = s.scalars(stmt).all()
    return [out(o) for o in rows]


@app.get("/admin/stats", tags=["admin"])
def stats(_: Identity = Depends(admin_identity)) -> dict:
    with Session() as s:
        by_status = dict(s.execute(select(Order.status, func.count()).group_by(Order.status)).all())
        revenue = s.scalar(select(func.sum(Order.total_cents)).where(
            Order.status.in_(("paid", "confirmed", "shipped", "delivered"))))
    return {"orders_by_status": by_status, "revenue_cents": revenue or 0}


@app.post("/admin/orders/{order_id}/ship", response_model=OrderOut, tags=["admin"])
def ship(order_id: int, _: Identity = Depends(admin_identity)) -> OrderOut:
    order = transition(order_id, ("paid", "confirmed"), "shipped",
                       emit=lambda o: [("order.shipped", {"order_id": o.id, "user_id": o.user_id})])
    if order is None:
        raise ServiceError(409, "invalid_state", "Only paid or confirmed orders can be shipped")
    return out(order)


@app.post("/admin/orders/{order_id}/deliver", response_model=OrderOut, tags=["admin"])
def deliver(order_id: int, _: Identity = Depends(admin_identity)) -> OrderOut:
    order = transition(order_id, ("shipped",), "delivered",
                       emit=lambda o: [("order.delivered", {"order_id": o.id, "user_id": o.user_id})])
    if order is None:
        raise ServiceError(409, "invalid_state", "Only shipped orders can be delivered")
    return out(order)
