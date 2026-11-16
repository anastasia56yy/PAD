"""inventory-service: stock levels and reservations for orders.

Owns: inventory_db.
Consumes:
  book.created     -> creates the stock record with the initial quantity
  order.created    -> reserves every line of the order, all or nothing
  order.paid       -> reserved items become sold
  order.cancelled  -> reserved items return to the shelf
Publishes: inventory.reserved / inventory.rejected (step of the checkout saga)

Reservation is one conditional UPDATE per line (available >= qty) inside one transaction:
two buyers can never take the last copy at the same time, and a partly available order reserves nothing.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import events, outbox
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.logging import log_event
from utm_common.security import Identity, admin_identity
from utm_common.web import ServiceError, create_app

log = logging.getLogger("inventory")


class Stock(Base):
    __tablename__ = "stock"
    book_id: Mapped[int] = mapped_column(primary_key=True)
    available: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    sold: Mapped[int] = mapped_column(Integer, default=0)


class Reservation(Base):
    __tablename__ = "reservations"
    order_id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))  # reserved | sold | released


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("inventory-service", {"database": lambda: ping(engine)},
                 description="Stock levels and order reservations.")


# ---------- event handlers ----------
def on_book_created(event: dict) -> None:
    p = event["payload"]
    with Session() as s:
        if s.get(Stock, p["book_id"]) is None:
            s.add(Stock(book_id=p["book_id"], available=p.get("initial_stock", 0)))
            s.commit()


def reserve(order_id: int, items: list[dict]) -> tuple[bool, list[dict]]:
    """All-or-nothing reservation; the outcome event is written to the outbox in the same transaction.
    Returns (ok, lines that were short)."""
    with Session() as s:
        if s.scalar(select(Reservation.order_id).where(Reservation.order_id == order_id)):
            return True, []  # duplicate delivery of order.created: already reserved and announced
        short = []
        for item in items:
            result = s.execute(update(Stock)
                               .where(Stock.book_id == item["book_id"], Stock.available >= item["quantity"])
                               .values(available=Stock.available - item["quantity"],
                                       reserved=Stock.reserved + item["quantity"]))
            if result.rowcount == 0:
                stock = s.get(Stock, item["book_id"])
                short.append({"book_id": item["book_id"], "requested": item["quantity"],
                              "available": stock.available if stock else 0})
            else:
                s.add(Reservation(order_id=order_id, book_id=item["book_id"], quantity=item["quantity"],
                                  status="reserved"))
        if short:
            s.rollback()  # nothing stays reserved for a partly available order
            outbox.add(s, "inventory.rejected", {"order_id": order_id, "short": short})
            s.commit()
            return False, short
        outbox.add(s, "inventory.reserved", {"order_id": order_id})
        s.commit()
        return True, []


def settle(order_id: int, new_status: str) -> None:
    """order.paid -> reserved lines become sold.
    order.cancelled -> reserved or sold lines go back on the shelf (an order can only be cancelled before
    shipping, e.g. cash-on-delivery, so its copies are still in the warehouse).
    Lines already in the target state are skipped, so a redelivered event changes nothing."""
    from_states = ("reserved",) if new_status == "sold" else ("reserved", "sold")
    with Session() as s:
        rows = s.scalars(select(Reservation).where(Reservation.order_id == order_id,
                                                   Reservation.status.in_(from_states))).all()
        for r in rows:
            if new_status == "sold":
                values = {"reserved": Stock.reserved - r.quantity, "sold": Stock.sold + r.quantity}
            elif r.status == "reserved":
                values = {"reserved": Stock.reserved - r.quantity, "available": Stock.available + r.quantity}
            else:  # sold -> back to available
                values = {"sold": Stock.sold - r.quantity, "available": Stock.available + r.quantity}
            s.execute(update(Stock).where(Stock.book_id == r.book_id).values(**values))
            r.status = new_status
        s.commit()
    if rows:
        log_event(log, f"Reservation {new_status}", order_id=order_id, lines=len(rows))


def on_order_event(event: dict) -> None:
    p, kind = event["payload"], event["type"]
    if kind == "order.created":
        ok, short = reserve(p["order_id"], p["items"])
        if ok:
            log_event(log, "Order reserved", order_id=p["order_id"])
        else:
            log_event(log, "Order rejected: out of stock", logging.WARNING, order_id=p["order_id"], short=short)
    elif kind == "order.paid":
        settle(p["order_id"], "sold")
    elif kind == "order.cancelled":
        settle(p["order_id"], "released")


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("inventory-service.catalog", on_book_created)
        events.start_consumer("inventory-service.orders", on_order_event)
        outbox.start_relay(Session)


# ---------- API ----------
class StockOut(BaseModel):
    book_id: int
    available: int
    reserved: int
    sold: int


class StockSet(BaseModel):
    available: int = Field(ge=0, le=100_000)


@app.get("/stock", response_model=list[StockOut], tags=["inventory"],
         summary="Stock of several books (the gateway joins it with the catalog)")
def stock(ids: str = Query(..., pattern=r"^\d+(,\d+)*$")) -> list[StockOut]:
    wanted = [int(x) for x in ids.split(",")][:200]
    with Session() as s:
        rows = s.scalars(select(Stock).where(Stock.book_id.in_(wanted))).all()
    return [StockOut.model_validate(r, from_attributes=True) for r in rows]


@app.put("/stock/{book_id}", response_model=StockOut, tags=["admin"], summary="Set available quantity (restock)")
def set_stock(book_id: int, body: StockSet, _: Identity = Depends(admin_identity)) -> StockOut:
    with Session() as s:
        row = s.get(Stock, book_id)
        if row is None:
            raise ServiceError(404, "not_found", f"No stock record for book {book_id}")
        row.available = body.available
        s.commit()
        s.refresh(row)
    log_event(log, "Stock set", book_id=book_id, available=body.available)
    return StockOut.model_validate(row, from_attributes=True)
