"""Asynchronous events over RabbitMQ (topic exchange "utm.events").

Every event: {"id", "type", "occurred_at", "request_id", "payload"}. Each consuming service owns a
durable queue bound to the routing keys it needs, so events wait in RabbitMQ while a consumer is down
and are processed when it comes back (at-least-once delivery: handlers must be idempotent).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

import pika

from .config import env
from .logging import log_event, request_id_var

log = logging.getLogger("events")
EXCHANGE = "utm.events"


def _params() -> pika.URLParameters:
    params = pika.URLParameters(env("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/"))
    params.heartbeat = 30
    params.blocked_connection_timeout = 10
    return params


def check_connection() -> None:
    """Readiness check: can we open a connection to the broker?"""
    conn = pika.BlockingConnection(_params())
    conn.close()


def publish(event_type: str, payload: dict, retries: int = 3) -> None:
    """Publish one persistent event. A short-lived connection per event is enough for this load and
    survives broker restarts without reconnection logic."""
    body = json.dumps({
        "id": uuid.uuid4().hex,
        "type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": request_id_var.get(),
        "payload": payload,
    }, ensure_ascii=False)
    for attempt in range(1, retries + 1):
        try:
            conn = pika.BlockingConnection(_params())
            ch = conn.channel()
            ch.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
            ch.basic_publish(EXCHANGE, event_type, body.encode(),
                             pika.BasicProperties(content_type="application/json", delivery_mode=2))
            conn.close()
            log_event(log, "Event published", event=event_type)
            return
        except pika.exceptions.AMQPError as exc:
            log_event(log, "Publish failed", logging.WARNING, event=event_type, attempt=attempt, error=str(exc))
            time.sleep(attempt)
    # the main operation already succeeded; losing a notification must not fail the user request
    log_event(log, "Event dropped after retries", logging.ERROR, event=event_type)


def start_consumer(queue: str, routing_keys: list[str], handler: Callable[[dict], None]) -> threading.Thread:
    """Consume in a daemon thread, reconnecting forever. handler(event) raising -> message is requeued once,
    then dropped to avoid an endless poison-message loop."""

    def run() -> None:
        while True:
            try:
                conn = pika.BlockingConnection(_params())
                ch = conn.channel()
                ch.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
                ch.queue_declare(queue, durable=True)
                for key in routing_keys:
                    ch.queue_bind(queue, EXCHANGE, routing_key=key)
                ch.basic_qos(prefetch_count=10)

                def on_message(channel, method, _props, body):
                    event = json.loads(body)
                    token = request_id_var.set(event.get("request_id") or "-")
                    try:
                        handler(event)
                        channel.basic_ack(method.delivery_tag)
                        log_event(log, "Event handled", event=event.get("type"), queue=queue)
                    except Exception:  # noqa: BLE001
                        log.exception("Event handler failed")
                        channel.basic_nack(method.delivery_tag, requeue=not method.redelivered)
                    finally:
                        request_id_var.reset(token)

                ch.basic_consume(queue, on_message)
                log_event(log, "Consumer started", queue=queue, keys=routing_keys)
                ch.start_consuming()
            except Exception as exc:  # noqa: BLE001 - broker restart, network: retry
                log_event(log, "Consumer disconnected, retrying", logging.WARNING, queue=queue, error=str(exc))
                time.sleep(5)

    thread = threading.Thread(target=run, name=f"consumer-{queue}", daemon=True)
    thread.start()
    return thread
