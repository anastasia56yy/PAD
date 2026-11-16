"""Asynchronous events over RabbitMQ (topic exchange "shop.events").

Every event: {"id", "type", "occurred_at", "request_id", "payload"}. Each consuming service owns a
durable queue bound to the routing keys it needs, so events wait in RabbitMQ while a consumer is down
and are processed when it comes back (at-least-once delivery: handlers must be idempotent).

The whole topology (queue -> routing keys) is declared here and by every publisher: a message published
before its consumer has ever started would otherwise be routed to no queue and silently lost.
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
EXCHANGE = "shop.events"

# queue (owned by one service) -> event types it receives
TOPOLOGY: dict[str, list[str]] = {
    "user-service.users": ["user.registered"],
    "inventory-service.catalog": ["book.created"],
    "inventory-service.orders": ["order.created", "order.paid", "order.cancelled"],
    "cart-service.orders": ["order.created"],
    "order-service.saga": ["inventory.reserved", "inventory.rejected", "payment.succeeded", "payment.failed"],
    "payment-service.requests": ["payment.requested"],
    "notification-service.all": ["user.registered", "order.created", "order.paid", "order.cancelled",
                                 "order.shipped", "order.delivered"],
}


def declare_topology(ch) -> None:
    ch.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
    for queue, keys in TOPOLOGY.items():
        ch.queue_declare(queue, durable=True)
        for key in keys:
            ch.queue_bind(queue, EXCHANGE, routing_key=key)


_declared = False


def _params() -> pika.URLParameters:
    params = pika.URLParameters(env("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/"))
    params.heartbeat = 30
    params.blocked_connection_timeout = 10
    return params


def check_connection() -> None:
    """Readiness check: can we open a connection to the broker?"""
    conn = pika.BlockingConnection(_params())
    conn.close()


_send_lock = threading.Lock()
_conn = None


def send_raw(event_type: str, body: str) -> None:
    """Send one already-serialized event over a cached connection. Raises on failure (used by the outbox relay,
    which keeps the row and retries)."""
    global _conn, _declared
    with _send_lock:
        try:
            if _conn is None or _conn.is_closed:
                _conn = pika.BlockingConnection(_params())
                _declared = False
            ch = _conn.channel()
            if not _declared:
                declare_topology(ch)
                _declared = True
            ch.basic_publish(EXCHANGE, event_type, body.encode(),
                             pika.BasicProperties(content_type="application/json", delivery_mode=2))
            ch.close()
        except Exception:
            try:
                if _conn is not None and not _conn.is_closed:
                    _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None
            raise
    log_event(log, "Event published", event=event_type)


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
    global _declared
    for attempt in range(1, retries + 1):
        try:
            conn = pika.BlockingConnection(_params())
            ch = conn.channel()
            if not _declared:
                declare_topology(ch)
                _declared = True
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


def start_consumer(queue: str, handler: Callable[[dict], None]) -> threading.Thread:
    """Consume `queue` (see TOPOLOGY) in a daemon thread, reconnecting forever. handler(event) raising ->
    the message is requeued once, then dropped to avoid an endless poison-message loop."""
    if queue not in TOPOLOGY:
        raise ValueError(f"Unknown queue {queue}: add it to TOPOLOGY")
    routing_keys = TOPOLOGY[queue]

    def run() -> None:
        while True:
            try:
                conn = pika.BlockingConnection(_params())
                ch = conn.channel()
                declare_topology(ch)
                ch.basic_qos(prefetch_count=1)  # one at a time: saga steps of one order stay in order

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
