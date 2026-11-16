"""auth-service: registration, login, JWT issuing.

Owns: auth_db (credentials only — profile data lives in user-service).
Publishes: user.registered
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import DateTime, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import outbox
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.logging import log_event
from utm_common.security import Identity, create_token, current_identity, hash_password, verify_password
from utm_common.web import ServiceError, create_app

log = logging.getLogger("auth")


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="customer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("auth-service", {"database": lambda: ping(engine)},
                 description="Registration, login and JWT access tokens.")

# comma-separated e-mails that get the admin role on registration (manage catalog, stock and orders)
ADMIN_EMAILS = {e.strip().lower() for e in env("ADMIN_EMAILS", "").split(",") if e.strip()}


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        outbox.start_relay(Session)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    role: str


@app.post("/auth/register", response_model=TokenOut, status_code=201, tags=["auth"])
def register(body: RegisterIn) -> TokenOut:
    email = body.email.lower()
    role = "admin" if email in ADMIN_EMAILS else "customer"
    with Session() as s:
        cred = Credential(email=email, password_hash=hash_password(body.password), role=role)
        s.add(cred)
        try:
            s.flush()  # assigns the id (and fails on a duplicate e-mail)
            # profile data goes to user-service asynchronously, through the outbox of this transaction
            outbox.add(s, "user.registered", {"user_id": cred.id, "email": email, "full_name": body.full_name})
            s.commit()
        except IntegrityError as exc:
            raise ServiceError(409, "email_taken", "An account with this e-mail already exists") from exc
    log_event(log, "User registered", user_id=cred.id, role=role)
    token, ttl = create_token(cred.id, email, role)
    return TokenOut(access_token=token, expires_in=ttl, user_id=cred.id, role=role)


@app.post("/auth/login", response_model=TokenOut, tags=["auth"])
def login(body: LoginIn) -> TokenOut:
    with Session() as s:
        cred = s.scalar(select(Credential).where(Credential.email == body.email.lower()))
    if cred is None or not verify_password(body.password, cred.password_hash):
        # same answer for unknown e-mail and wrong password: do not reveal which accounts exist
        raise ServiceError(401, "invalid_credentials", "Wrong e-mail or password")
    token, ttl = create_token(cred.id, cred.email, cred.role)
    log_event(log, "User logged in", user_id=cred.id)
    return TokenOut(access_token=token, expires_in=ttl, user_id=cred.id, role=cred.role)


@app.get("/auth/me", tags=["auth"])
def me(identity: Identity = Depends(current_identity)) -> dict:
    with Session() as s:
        cred = s.get(Credential, identity.user_id)
    if cred is None:
        raise ServiceError(404, "not_found", "Account not found")
    return {"user_id": cred.id, "email": cred.email, "role": cred.role, "created_at": cred.created_at.isoformat()}
