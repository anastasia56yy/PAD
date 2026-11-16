"""user-service: customer profiles with the default delivery address.

Owns: user_db.
Consumes: user.registered (creates the profile — registration in auth-service does not call us directly).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import events
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.logging import log_event
from utm_common.security import Identity, current_identity
from utm_common.web import ServiceError, create_app

log = logging.getLogger("users")


class Profile(Base):
    __tablename__ = "profiles"
    user_id: Mapped[int] = mapped_column(primary_key=True)  # same id as in auth-service
    email: Mapped[str] = mapped_column(String(254))
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    street: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("user-service", {"database": lambda: ping(engine)},
                 description="Customer profiles and default delivery address.")


def on_user_registered(event: dict) -> None:
    p = event["payload"]
    with Session() as s:
        if s.get(Profile, p["user_id"]):  # at-least-once delivery: the event may arrive twice
            return
        s.add(Profile(user_id=p["user_id"], email=p["email"], full_name=p["full_name"]))
        s.commit()
    log_event(log, "Profile created", user_id=p["user_id"])


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("user-service.users", on_user_registered)


class ProfileOut(BaseModel):
    user_id: int
    email: str
    full_name: str
    phone: str | None
    city: str | None
    street: str | None


class ProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9 ()-]{6,20}$")
    city: str | None = Field(default=None, min_length=2, max_length=80)
    street: str | None = Field(default=None, min_length=3, max_length=200)


def _get(user_id: int) -> Profile:
    with Session() as s:
        profile = s.get(Profile, user_id)
    if profile is None:
        # right after registration the event may not be processed yet
        raise ServiceError(404, "not_found", "Profile not found (it is created a moment after registration)")
    return profile


@app.get("/users/me", response_model=ProfileOut, tags=["users"])
def my_profile(identity: Identity = Depends(current_identity)) -> ProfileOut:
    return ProfileOut.model_validate(_get(identity.user_id), from_attributes=True)


@app.patch("/users/me", response_model=ProfileOut, tags=["users"], summary="Update name, phone or address")
def update_profile(body: ProfileUpdate, identity: Identity = Depends(current_identity)) -> ProfileOut:
    with Session() as s:
        profile = s.get(Profile, identity.user_id)
        if profile is None:
            raise ServiceError(404, "not_found", "Profile not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(profile, field, value)
        s.commit()
        s.refresh(profile)
    log_event(log, "Profile updated", user_id=identity.user_id)
    return ProfileOut.model_validate(profile, from_attributes=True)


@app.get("/users/{user_id}", response_model=ProfileOut, tags=["users"])
def get_profile(user_id: int, identity: Identity = Depends(current_identity)) -> ProfileOut:
    if identity.user_id != user_id and identity.role != "admin":
        raise ServiceError(403, "forbidden", "You can only view your own profile")
    return ProfileOut.model_validate(_get(user_id), from_attributes=True)
