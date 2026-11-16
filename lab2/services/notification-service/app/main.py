"""notification-service: in-app notifications for customers.

Owns: notification_db.
Consumes:
  user.registered  -> welcome notification
  order.created    -> "order received, checking stock"
  order.paid       -> "order paid" / "order confirmed, pay on delivery"
  order.cancelled  -> "order cancelled" with the reason (out of stock, card declined, by customer)
  order.shipped    -> "order shipped"
  order.delivered  -> "order delivered"
A notification with user_id = NULL is shown to everybody (kept for shop-wide announcements).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import DateTime, Integer, String, Text, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import events
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.logging import log_event
from utm_common.security import Identity, current_identity
from utm_common.web import ServiceError, create_app

log = logging.getLogger("notifications")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True)  # one notification per event
    user_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)  # NULL = broadcast
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ReadMark(Base):
    """Read state per user: a broadcast notification read by one customer stays unread for the others."""
    __tablename__ = "read_marks"
    notification_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("notification-service", {"database": lambda: ping(engine)}, description="In-app notifications.")


def money(cents: int | None) -> str:
    return f"{(cents or 0) / 100:,.2f} MDL".replace(",", " ")


def build_notification(event: dict) -> Notification | None:
    p, kind = event["payload"], event["type"]

    def note(kind_: str, title: str, body: str) -> Notification:
        return Notification(event_id=event["id"], user_id=p.get("user_id"), kind=kind_, title=title, body=body)

    if kind == "user.registered":
        return note("welcome", "Добро пожаловать в Libraria",
                    "Выбирайте книги в каталоге, добавляйте в корзину и следите за заказами здесь.")
    oid = p.get("order_id")
    if kind == "order.created":
        return note("order_created", f"Заказ №{oid} принят",
                    f"Сумма {money(p.get('total_cents'))}. Проверяем наличие на складе.")
    if kind == "order.paid":
        if p.get("status") == "confirmed":
            return note("order_confirmed", f"Заказ №{oid} подтверждён",
                        f"Оплата при получении: {money(p.get('total_cents'))}. Готовим к отправке.")
        return note("order_paid", f"Заказ №{oid} оплачен",
                    f"Платёж {money(p.get('total_cents'))} прошёл. Готовим к отправке.")
    if kind == "order.cancelled":
        return note("order_cancelled", f"Заказ №{oid} отменён", p.get("reason_text") or "Заказ отменён.")
    if kind == "order.shipped":
        return note("order_shipped", f"Заказ №{oid} отправлен", "Курьер свяжется с вами перед доставкой.")
    if kind == "order.delivered":
        return note("order_delivered", f"Заказ №{oid} доставлен", "Спасибо за покупку! Приятного чтения.")
    return None


def on_event(event: dict) -> None:
    notification = build_notification(event)
    if notification is None:
        return
    with Session() as s:
        if s.scalar(select(Notification.id).where(Notification.event_id == event["id"])):
            return
        s.add(notification)
        s.commit()
    log_event(log, "Notification created", kind=notification.kind, user_id=notification.user_id)


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("notification-service.all", on_event)


class NotificationOut(BaseModel):
    id: int
    kind: str
    title: str
    body: str
    read: bool
    created_at: datetime


def _visible_to(user_id: int):
    return or_(Notification.user_id == user_id, Notification.user_id.is_(None))


@app.get("/notifications", response_model=list[NotificationOut], tags=["notifications"])
def my_notifications(unread_only: bool = Query(False), identity: Identity = Depends(current_identity)):
    with Session() as s:
        rows = s.scalars(select(Notification).where(_visible_to(identity.user_id))
                         .order_by(Notification.created_at.desc()).limit(50)).all()
        read_ids = set(s.scalars(select(ReadMark.notification_id).where(ReadMark.user_id == identity.user_id)))
    items = [NotificationOut(id=r.id, kind=r.kind, title=r.title, body=r.body, read=r.id in read_ids,
                             created_at=r.created_at) for r in rows]
    return [n for n in items if not n.read] if unread_only else items


@app.post("/notifications/{notification_id}/read", status_code=204, response_class=Response, tags=["notifications"])
def mark_read(notification_id: int, identity: Identity = Depends(current_identity)):
    with Session() as s:
        exists = s.scalar(select(Notification.id).where(Notification.id == notification_id,
                                                        _visible_to(identity.user_id)))
        if exists is None:
            raise ServiceError(404, "not_found", "Notification not found")
        if s.get(ReadMark, (notification_id, identity.user_id)) is None:
            s.add(ReadMark(notification_id=notification_id, user_id=identity.user_id))
            s.commit()
