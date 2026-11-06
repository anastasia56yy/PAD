"""history-service: every question a student asked and the answer they got.

Owns: history_db.
Consumes: question.answered (answer-service does not wait for the history to be written).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import events
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.logging import log_event
from utm_common.security import Identity, current_identity
from utm_common.web import ServiceError, create_app

log = logging.getLogger("history")


class Entry(Base):
    __tablename__ = "entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True)  # idempotency key
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    refused: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[str] = mapped_column(String(10), default="high")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    latency_s: Mapped[float] = mapped_column(Float, default=0.0)
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("history-service", {"database": lambda: ping(engine)}, description="Question history per student.")


def on_question_answered(event: dict) -> None:
    p = event["payload"]
    if not p.get("user_id"):
        return
    with Session() as s:
        if s.scalar(select(Entry.id).where(Entry.event_id == event["id"])):
            return  # duplicate delivery
        s.add(Entry(event_id=event["id"], user_id=p["user_id"], question=p["question"], answer=p["answer"],
                    refused=p.get("refused", False), confidence=p.get("confidence", "high"),
                    sources=[{"title": x.get("title"), "url": x.get("url")} for x in p.get("sources", [])],
                    latency_s=p.get("latency_s", 0.0)))
        s.commit()
    log_event(log, "History entry saved", user_id=p["user_id"])


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("history-service.question-answered", ["question.answered"], on_question_answered)


class EntryOut(BaseModel):
    id: int
    question: str
    answer: str
    refused: bool
    confidence: str
    sources: list
    latency_s: float
    asked_at: datetime


class HistoryPage(BaseModel):
    total: int
    items: list[EntryOut]


@app.get("/history", response_model=HistoryPage, tags=["history"])
def my_history(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
               identity: Identity = Depends(current_identity)) -> HistoryPage:
    with Session() as s:
        total = s.scalar(select(func.count()).select_from(Entry).where(Entry.user_id == identity.user_id))
        rows = s.scalars(select(Entry).where(Entry.user_id == identity.user_id)
                         .order_by(Entry.asked_at.desc()).limit(limit).offset(offset)).all()
    return HistoryPage(total=total or 0, items=[EntryOut.model_validate(r, from_attributes=True) for r in rows])


@app.get("/history/stats", tags=["history"])
def my_stats(identity: Identity = Depends(current_identity)) -> dict:
    with Session() as s:
        q = select(func.count(), func.sum(func.cast(Entry.refused, Integer)), func.avg(Entry.latency_s)) \
            .where(Entry.user_id == identity.user_id)
        total, refused, avg_latency = s.execute(q).one()
    return {"questions": total or 0, "refused": int(refused or 0), "avg_latency_s": round(avg_latency or 0, 2)}


@app.delete("/history/{entry_id}", status_code=204, response_class=Response, tags=["history"])
def delete_entry(entry_id: int, identity: Identity = Depends(current_identity)):
    with Session() as s:
        result = s.execute(delete(Entry).where(Entry.id == entry_id, Entry.user_id == identity.user_id))
        s.commit()
    if result.rowcount == 0:
        raise ServiceError(404, "not_found", "History entry not found")
