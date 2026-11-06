"""SQLAlchemy engine/session per service. Each service has its own database and credentials
(DATABASE_URL), so no service can read another one's tables."""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import env
from .logging import log_event

log = logging.getLogger("db")


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None) -> Engine:
    url = url or env("DATABASE_URL", required=True)
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):  # tests
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def make_session_factory(engine: Engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_schema(engine: Engine, base=Base, attempts: int = 30) -> None:
    """Create tables, waiting for the database to accept connections (it may start after the service)."""
    for attempt in range(1, attempts + 1):
        try:
            base.metadata.create_all(engine)
            return
        except Exception as exc:  # noqa: BLE001
            log_event(log, "Database not ready, retrying", logging.WARNING, attempt=attempt, error=str(exc)[:120])
            time.sleep(2)
    raise RuntimeError("Database is not reachable")


def ping(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
