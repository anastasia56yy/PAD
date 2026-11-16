"""Transactional outbox.

Problem: "save the order, then publish order.created" can lose the event — the broker may be down or
restarting (it happened at startup: book.created was dropped after three retries and one book got no stock).
Solution: the event is inserted into the service's own `outbox` table in the SAME database transaction as
the business change. Either both are saved or neither. A relay thread then sends unsent rows to RabbitMQ in
order and marks them sent, retrying for as long as the broker is unavailable.
Guarantee: at-least-once publication (a row may be sent twice if the service dies right after sending),
which consumers already handle by being idempotent.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import events
from .db import Base
from .logging import log_event, request_id_var

log = logging.getLogger("outbox")


class OutboxEvent(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(60))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


def add(session: Session, event_type: str, payload: dict) -> None:
    """Queue an event inside the caller's transaction (it is sent only if the transaction commits)."""
    body = json.dumps({
        "id": uuid.uuid4().hex,
        "type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": request_id_var.get(),
        "payload": payload,
    }, ensure_ascii=False)
    session.add(OutboxEvent(event_type=event_type, body=body))


def relay_once(session_factory) -> int:
    """Send pending events in creation order; stop at the first failure (order is preserved). Returns sent count."""
    sent = 0
    with session_factory() as s:
        # FOR UPDATE SKIP LOCKED: with several replicas of a service, each pending row is taken by one relay only
        # (PostgreSQL; SQLite in tests ignores the clause)
        rows = s.scalars(select(OutboxEvent).where(OutboxEvent.sent_at.is_(None))
                         .order_by(OutboxEvent.id).limit(100).with_for_update(skip_locked=True)).all()
        for row in rows:
            events.send_raw(row.event_type, row.body)  # raises when the broker is unavailable
            row.sent_at = datetime.now(timezone.utc)
            s.commit()
            sent += 1
    return sent


def start_relay(session_factory, interval_s: float = 0.3) -> threading.Thread:
    def run() -> None:
        failures = 0
        while True:
            try:
                relay_once(session_factory)
                if failures:
                    log_event(log, "Outbox relay recovered", after_failures=failures)
                failures = 0
                time.sleep(interval_s)
            except Exception as exc:  # noqa: BLE001 - broker or DB down: keep the rows, retry later
                failures += 1
                if failures in (1, 10) or failures % 100 == 0:
                    log_event(log, "Outbox relay waiting for the broker", logging.WARNING, failures=failures,
                              error=repr(exc)[:200])
                time.sleep(min(2.0, 0.2 * failures))

    thread = threading.Thread(target=run, name="outbox-relay", daemon=True)
    thread.start()
    return thread
