"""notification-service: in-app notifications for students.

Owns: notification_db.
Consumes:
  user.registered    -> welcome notification
  question.answered  -> when the base had no answer, suggest how to rephrase
  documents.updated  -> broadcast "the document base was updated" (user_id = NULL means everyone)
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
    """Read state per user: a broadcast notification read by one student stays unread for the others."""
    __tablename__ = "read_marks"
    notification_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("notification-service", {"database": lambda: ping(engine)}, description="In-app notifications.")


def build_notification(event: dict) -> Notification | None:
    p, kind = event["payload"], event["type"]
    if kind == "user.registered":
        return Notification(event_id=event["id"], user_id=p["user_id"], kind="welcome",
                            title="Добро пожаловать в UTM Assistant",
                            body="Задавайте вопросы о регламентах, ECTS, стипендиях и факультете — "
                                 "ответы приходят из официальных документов со ссылками на источники.")
    if kind == "question.answered" and p.get("refused") and p.get("user_id"):
        return Notification(event_id=event["id"], user_id=p["user_id"], kind="no_answer",
                            title="На вопрос не нашлось ответа",
                            body=f"«{p['question'][:120]}» — в документах UTM нет ответа. "
                                 "Попробуйте сформулировать конкретнее или спросить о другом аспекте.")
    if kind == "documents.updated":
        return Notification(event_id=event["id"], user_id=None, kind="documents_updated",
                            title="База документов обновлена",
                            body=f"Новых документов: {p.get('new', 0)}, изменённых: {p.get('updated', 0)}, "
                                 f"удалённых: {p.get('deleted', 0)}. Всего в базе: {p.get('documents', '—')}.")
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
        events.start_consumer("notification-service.all",
                              ["user.registered", "question.answered", "documents.updated"], on_event)


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
