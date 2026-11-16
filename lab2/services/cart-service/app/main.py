"""cart-service: the shopping cart of each buyer.

Owns: cart_db (only book ids and quantities: titles and prices are always taken fresh from the catalog,
so a changed price is never shown from a stale copy).
Calls: catalog-service (REST) to show titles, prices and to validate added books.
Consumes: order.paid -> removes the bought books from the cart. Not order.created: if the card is declined or a
          book is out of stock the order is cancelled, and the buyer should still find the books in the cart.
"""
from __future__ import annotations

import logging

from fastapi import Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import Integer, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import events
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.http import call
from utm_common.logging import log_event
from utm_common.security import Identity, current_identity
from utm_common.web import ServiceError, create_app

log = logging.getLogger("cart")
CATALOG_URL = env("CATALOG_URL", "http://catalog-service:8000")
MAX_QTY = 10


class CartItem(Base):
    __tablename__ = "cart_items"
    user_id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("cart-service", {"database": lambda: ping(engine)}, description="Shopping carts.")


def on_order_paid(event: dict) -> None:
    p = event["payload"]
    ordered = [i["book_id"] for i in p["items"]]
    with Session() as s:
        s.execute(delete(CartItem).where(CartItem.user_id == p["user_id"], CartItem.book_id.in_(ordered)))
        s.commit()
    log_event(log, "Cart emptied after order", user_id=p["user_id"], order_id=p["order_id"])


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("cart-service.orders", on_order_paid)


def fetch_books(ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    resp = call("GET", f"{CATALOG_URL}/books-batch", params={"ids": ",".join(map(str, ids))}, timeout=5)
    if resp.status_code != 200:
        raise ServiceError(503, "catalog_unavailable", "Catalog is unavailable")
    return {b["id"]: b for b in resp.json()}


class LineOut(BaseModel):
    book_id: int
    title: str
    author: str
    cover_color: str
    unit_price_cents: int
    quantity: int
    line_total_cents: int


class CartOut(BaseModel):
    items: list[LineOut]
    total_cents: int
    count: int


class QuantityIn(BaseModel):
    quantity: int = Field(ge=1, le=MAX_QTY)


def build_cart(user_id: int) -> CartOut:
    with Session() as s:
        rows = s.scalars(select(CartItem).where(CartItem.user_id == user_id).order_by(CartItem.book_id)).all()
    books = fetch_books([r.book_id for r in rows])
    lines = []
    for r in rows:
        b = books.get(r.book_id)
        if b is None:  # removed from the catalog meanwhile: skip it rather than fail the whole cart
            continue
        lines.append(LineOut(book_id=r.book_id, title=b["title"], author=b["author"], cover_color=b["cover_color"],
                             unit_price_cents=b["price_cents"], quantity=r.quantity,
                             line_total_cents=b["price_cents"] * r.quantity))
    return CartOut(items=lines, total_cents=sum(x.line_total_cents for x in lines),
                   count=sum(x.quantity for x in lines))


@app.get("/cart", response_model=CartOut, tags=["cart"])
def get_cart(identity: Identity = Depends(current_identity)) -> CartOut:
    return build_cart(identity.user_id)


@app.put("/cart/items/{book_id}", response_model=CartOut, tags=["cart"], summary="Add a book or set its quantity")
def set_item(book_id: int, body: QuantityIn, identity: Identity = Depends(current_identity)) -> CartOut:
    if book_id not in fetch_books([book_id]):
        raise ServiceError(404, "not_found", f"Book {book_id} not found")
    with Session() as s:
        item = s.get(CartItem, (identity.user_id, book_id))
        if item is None:
            s.add(CartItem(user_id=identity.user_id, book_id=book_id, quantity=body.quantity))
        else:
            item.quantity = body.quantity
        s.commit()
    log_event(log, "Cart item set", book_id=book_id, quantity=body.quantity)
    return build_cart(identity.user_id)


@app.delete("/cart/items/{book_id}", response_model=CartOut, tags=["cart"])
def remove_item(book_id: int, identity: Identity = Depends(current_identity)) -> CartOut:
    with Session() as s:
        s.execute(delete(CartItem).where(CartItem.user_id == identity.user_id, CartItem.book_id == book_id))
        s.commit()
    return build_cart(identity.user_id)


@app.delete("/cart", status_code=204, response_class=Response, tags=["cart"])
def clear_cart(identity: Identity = Depends(current_identity)):
    with Session() as s:
        s.execute(delete(CartItem).where(CartItem.user_id == identity.user_id))
        s.commit()
