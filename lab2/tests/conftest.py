"""Unit tests run without Docker: every service gets an in-memory SQLite database, RabbitMQ is
replaced by a recorder, and each service package (all named `app`) is loaded under a unique name."""
from __future__ import annotations

import importlib
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
os.environ["PAYMENT_PROVIDER_DELAY_SECONDS"] = "0"


@pytest.fixture
def published(monkeypatch):
    """Collects events (written to the outbox or published directly) instead of sending them to RabbitMQ."""
    from utm_common import events, outbox

    sent = []
    monkeypatch.setattr(events, "publish", lambda event_type, payload, retries=3: sent.append((event_type, payload)))
    monkeypatch.setattr(outbox, "add", lambda session, event_type, payload: sent.append((event_type, payload)))
    return sent


def load_service(folder: str):
    """Import <folder>/app as a fresh package with its own in-memory database; returns its main module."""
    import sqlalchemy

    from utm_common import db
    from utm_common.db import Base

    # every service declares its own tables on the shared Base: forget the previously loaded service
    # (in Docker each service is a separate process, so this only matters in tests)
    Base.registry.dispose()
    Base.registry._class_registry.clear()
    Base.metadata.clear()
    from utm_common import outbox
    recorder = outbox.add  # the `published` fixture may have replaced it already
    importlib.reload(outbox)  # re-declare the outbox table on the cleared Base
    if getattr(recorder, "__module__", None) != outbox.__name__:
        outbox.add = recorder
    original = db.create_engine
    db.create_engine = lambda url, **kw: sqlalchemy.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        pkg = "svc_" + folder.replace("/", "_").replace("-", "_")
        for name in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
            del sys.modules[name]
        app_dir = LAB2 / folder / "app"
        spec = importlib.util.spec_from_file_location(pkg, app_dir / "__init__.py",
                                                      submodule_search_locations=[str(app_dir)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = package  # SQLAlchemy resolves Mapped[...] annotations through sys.modules
        spec.loader.exec_module(package)
        module = importlib.import_module(f"{pkg}.main")
    finally:
        db.create_engine = original
    if hasattr(module, "engine"):
        Base.metadata.create_all(module.engine)
    return module


def auth_headers(user_id: int = 1, role: str = "customer") -> dict:
    return {"X-User-Id": str(user_id), "X-User-Role": role}


def event(kind: str, payload: dict, event_id: str | None = None) -> dict:
    return {"id": event_id or f"{kind}-{payload.get('order_id', payload.get('book_id', 0))}", "type": kind,
            "payload": payload}
