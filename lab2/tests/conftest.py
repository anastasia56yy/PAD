"""Unit tests run without Docker: every service gets an in-memory SQLite database, RabbitMQ is
replaced by a recorder, and service modules are loaded from their folders under unique names
(they are all called app/main.py)."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool

LAB2 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB2 / "common"))

os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret")
os.environ["EVENTS_ENABLED"] = "false"
os.environ["DATABASE_URL"] = "sqlite://"


@pytest.fixture
def published(monkeypatch):
    """Collects events instead of sending them to RabbitMQ."""
    from utm_common import events

    sent = []
    monkeypatch.setattr(events, "publish", lambda event_type, payload, retries=3: sent.append((event_type, payload)))
    return sent


def load_service(folder: str):
    """Import <folder>/app/main.py as a fresh module with its own in-memory database."""
    import sqlalchemy

    from utm_common import db
    from utm_common.db import Base

    Base.metadata.clear()  # every service declares its own tables on the shared Base
    original = db.create_engine
    db.create_engine = lambda url, **kw: sqlalchemy.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        name = "svc_" + folder.replace("/", "_").replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, LAB2 / folder / "app" / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # SQLAlchemy resolves Mapped[...] annotations through sys.modules
        spec.loader.exec_module(module)
    finally:
        db.create_engine = original
    Base.metadata.create_all(module.engine) if hasattr(module, "engine") else None
    return module


def auth_headers(user_id: int = 1, role: str = "student") -> dict:
    return {"X-User-Id": str(user_id), "X-User-Role": role}
