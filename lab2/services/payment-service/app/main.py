"""payment-service: processes payments of orders (simulated payment provider).

Owns: payment_db.
Consumes: payment.requested
Publishes: payment.succeeded / payment.failed

No real card data is ever accepted: the buyer picks a test method at checkout.
  card_success      -> the test card is charged
  card_decline      -> the test card is declined by the "bank" (shows the compensation path of the saga)
  cash_on_delivery  -> nothing is charged now; the order is confirmed and paid to the courier
One payment per order (unique order_id): a redelivered request never charges twice.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import events, outbox
from utm_common.config import env, env_float
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.logging import log_event
from utm_common.security import Identity, current_identity
from utm_common.web import ServiceError, create_app

log = logging.getLogger("payments")
PROVIDER_DELAY = env_float("PAYMENT_PROVIDER_DELAY_SECONDS", 1.0)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))  # succeeded | failed | due_on_delivery
    failure_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=lambda: datetime.now(timezone.utc))


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("payment-service", {"database": lambda: ping(engine)},
                 description="Payments of orders through a simulated provider.")


def charge(method: str) -> tuple[str, str | None]:
    """The simulated provider: (status, failure reason)."""
    time.sleep(PROVIDER_DELAY)  # a real provider answers in about a second
    if method == "card_success":
        return "succeeded", None
    if method == "card_decline":
        return "failed", "Card declined by the issuing bank (test card)"
    if method == "cash_on_delivery":
        return "due_on_delivery", None
    return "failed", f"Unknown payment method {method}"


def outcome_event(s, payment: Payment) -> None:
    if payment.status == "failed":
        outbox.add(s, "payment.failed", {"order_id": payment.order_id, "reason": payment.failure_reason})
    else:
        outbox.add(s, "payment.succeeded", {"order_id": payment.order_id, "payment_id": payment.id,
                                            "method": payment.method})


def on_payment_requested(event: dict) -> None:
    p = event["payload"]
    with Session() as s:
        existing = s.scalar(select(Payment).where(Payment.order_id == p["order_id"]))
        if existing is not None:
            # redelivered request: never charge twice, just announce the stored result again
            # (the order ignores an outcome it already has)
            outcome_event(s, existing)
            s.commit()
            return
    status, reason = charge(p["method"])
    with Session() as s:
        payment = Payment(id=uuid.uuid4().hex, order_id=p["order_id"], user_id=p["user_id"],
                          amount_cents=p["amount_cents"], method=p["method"], status=status, failure_reason=reason)
        s.add(payment)
        outcome_event(s, payment)  # the payment and its event are saved together
        s.commit()
    log_event(log, "Payment processed", order_id=p["order_id"], status=status, amount_cents=p["amount_cents"])


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("payment-service.requests", on_payment_requested)
        outbox.start_relay(Session)


class PaymentOut(BaseModel):
    id: str
    order_id: int
    amount_cents: int
    method: str
    status: str
    failure_reason: str | None
    created_at: datetime


@app.get("/payments", response_model=PaymentOut, tags=["payments"], summary="Payment of an order")
def payment_of_order(order_id: int = Query(...), identity: Identity = Depends(current_identity)) -> PaymentOut:
    with Session() as s:
        payment = s.scalar(select(Payment).where(Payment.order_id == order_id))
    if payment is None or (payment.user_id != identity.user_id and identity.role != "admin"):
        raise ServiceError(404, "not_found", "No payment for this order")
    return PaymentOut.model_validate(payment, from_attributes=True)
