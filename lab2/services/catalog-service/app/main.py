"""catalog-service: books, genres, search.

Owns: catalog_db.
Publishes: book.created (inventory-service creates the stock record for the new book).
The price shown to the buyer comes from here; stock levels belong to inventory-service.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Integer, String, Text, func, or_, select, text
from sqlalchemy.orm import Mapped, mapped_column

from utm_common import outbox
from utm_common.config import env
from utm_common.db import Base, init_schema, make_engine, make_session_factory, ping
from utm_common.logging import log_event
from utm_common.security import Identity, admin_identity
from utm_common.web import ServiceError, create_app

from .seed import BOOKS, COVER_COLORS

log = logging.getLogger("catalog")


class Book(Base):
    __tablename__ = "books"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    author: Mapped[str] = mapped_column(String(200), index=True)
    genre: Mapped[str] = mapped_column(String(60), index=True)
    year: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(2))
    pages: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)  # money as integer bani: no float rounding errors
    description: Mapped[str] = mapped_column(Text, default="")
    cover_color: Mapped[str] = mapped_column(String(7), default="#1d3557")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine = make_engine()
Session = make_session_factory(engine)
app = create_app("catalog-service", {"database": lambda: ping(engine)},
                 description="Book catalog: listing, search, genres, admin management.")


def seed_catalog() -> None:
    """Fill an empty catalog once; inventory gets the starting stock through book.created events."""
    with Session() as s:
        if s.bind.dialect.name == "postgresql":
            # several replicas start at once (Kubernetes): only one of them may seed the empty catalog
            s.execute(text("SELECT pg_advisory_xact_lock(424242)"))
        if s.scalar(select(func.count()).select_from(Book)):
            return
        for i, (title, author, genre, year, lang, pages, price, stock, desc) in enumerate(BOOKS):
            book = Book(title=title, author=author, genre=genre, year=year, language=lang, pages=pages,
                        price_cents=price * 100, description=desc, cover_color=COVER_COLORS[i % len(COVER_COLORS)])
            s.add(book)
            s.flush()
            # same transaction as the book: no book without its event, no event without its book
            outbox.add(s, "book.created", {"book_id": book.id, "title": title, "initial_stock": stock})
        s.commit()
    log_event(log, "Catalog seeded", books=len(BOOKS))


@app.on_event("startup")
def startup() -> None:
    init_schema(engine)
    if env("SEED_CATALOG", "true") == "true":
        seed_catalog()
    if env("EVENTS_ENABLED", "true") == "true":
        outbox.start_relay(Session)


class BookOut(BaseModel):
    id: int
    title: str
    author: str
    genre: str
    year: int
    language: str
    pages: int
    price_cents: int
    description: str
    cover_color: str


class BookPage(BaseModel):
    total: int
    items: list[BookOut]


class BookIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    author: str = Field(min_length=1, max_length=200)
    genre: str = Field(min_length=2, max_length=60)
    year: int = Field(ge=1450, le=2100)
    language: str = Field(pattern="^(ro|en|ru)$")
    pages: int = Field(ge=1, le=10000)
    price_cents: int = Field(gt=0, le=10_000_000)
    description: str = Field(default="", max_length=2000)
    initial_stock: int = Field(default=0, ge=0, le=100_000)


class BookUpdate(BaseModel):
    price_cents: int | None = Field(default=None, gt=0, le=10_000_000)
    description: str | None = Field(default=None, max_length=2000)
    active: bool | None = None


SORTS = {"title": Book.title, "price": Book.price_cents, "-price": Book.price_cents.desc(),
         "year": Book.year, "-year": Book.year.desc()}


@app.get("/books", response_model=BookPage, tags=["catalog"], summary="List / search books")
def list_books(q: str | None = Query(None, max_length=100, description="Search in title and author"),
               genre: str | None = None, language: str | None = Query(None, pattern="^(ro|en|ru)$"),
               sort: str = Query("title", pattern="^(title|price|-price|year|-year)$"),
               limit: int = Query(24, ge=1, le=100), offset: int = Query(0, ge=0)) -> BookPage:
    stmt = select(Book).where(Book.active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Book.title.ilike(like), Book.author.ilike(like)))
    if genre:
        stmt = stmt.where(Book.genre == genre)
    if language:
        stmt = stmt.where(Book.language == language)
    with Session() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(SORTS[sort], Book.id).limit(limit).offset(offset)).all()
    return BookPage(total=total or 0, items=[BookOut.model_validate(b, from_attributes=True) for b in rows])


@app.get("/books/{book_id}", response_model=BookOut, tags=["catalog"])
def get_book(book_id: int) -> BookOut:
    with Session() as s:
        book = s.get(Book, book_id)
    if book is None or not book.active:
        raise ServiceError(404, "not_found", f"Book {book_id} not found")
    return BookOut.model_validate(book, from_attributes=True)


@app.get("/books-batch", response_model=list[BookOut], tags=["internal"],
         summary="Several books by id (used by cart and order services)")
def books_batch(ids: str = Query(..., pattern=r"^\d+(,\d+)*$")) -> list[BookOut]:
    wanted = [int(x) for x in ids.split(",")][:100]
    with Session() as s:
        rows = s.scalars(select(Book).where(Book.id.in_(wanted), Book.active.is_(True))).all()
    return [BookOut.model_validate(b, from_attributes=True) for b in rows]


@app.get("/genres", tags=["catalog"])
def genres() -> list[dict]:
    with Session() as s:
        rows = s.execute(select(Book.genre, func.count()).where(Book.active.is_(True))
                         .group_by(Book.genre).order_by(Book.genre)).all()
    return [{"genre": g, "books": n} for g, n in rows]


@app.post("/books", response_model=BookOut, status_code=201, tags=["admin"])
def create_book(body: BookIn, _: Identity = Depends(admin_identity)) -> BookOut:
    data = body.model_dump(exclude={"initial_stock"})
    with Session() as s:
        book = Book(**data, cover_color=COVER_COLORS[sum(map(ord, body.title)) % len(COVER_COLORS)])
        s.add(book)
        s.flush()
        outbox.add(s, "book.created", {"book_id": book.id, "title": book.title, "initial_stock": body.initial_stock})
        s.commit()
        s.refresh(book)
    log_event(log, "Book created", book_id=book.id)
    return BookOut.model_validate(book, from_attributes=True)


@app.patch("/books/{book_id}", response_model=BookOut, tags=["admin"])
def update_book(book_id: int, body: BookUpdate, _: Identity = Depends(admin_identity)) -> BookOut:
    with Session() as s:
        book = s.get(Book, book_id)
        if book is None:
            raise ServiceError(404, "not_found", f"Book {book_id} not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(book, field, value)
        s.commit()
        s.refresh(book)
    log_event(log, "Book updated", book_id=book_id)
    return BookOut.model_validate(book, from_attributes=True)
