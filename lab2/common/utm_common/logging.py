"""Structured JSON logs with timestamp, service, level and request id.

The request id comes from the X-Request-ID header (the gateway creates it), is kept in a
contextvar for the duration of the request and is forwarded to downstream calls and events,
so one user question can be traced through every service's logs.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from .config import LOG_LEVEL, SERVICE_NAME

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
REQUEST_ID_HEADER = "X-Request-ID"


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "service": SERVICE_NAME,
            "level": record.levelname,
            "request_id": request_id_var.get(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            entry.update(extra)
        if record.exc_info:
            # full traceback stays in the logs, never in an HTTP response
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(LOG_LEVEL)
    for noisy in ("uvicorn.access", "pika", "httpx", "httpcore", "chromadb", "sentence_transformers",
                  "huggingface_hub", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields) -> None:
    """logger.info with extra structured fields: log_event(log, "Order created", order_id=5)."""
    logger.log(level, message, extra={"fields": fields})
