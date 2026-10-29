"""SQLite manifest of grabbed documents.

It is what makes the grabber incremental: for each document it remembers the
`modified` timestamp from the source and a hash of the content, so a repeated
run downloads only new/changed documents and never stores duplicates.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,   -- site:type:source_id
    site         TEXT NOT NULL,
    doc_type     TEXT NOT NULL,      -- page | post | pdf
    url          TEXT NOT NULL,
    title        TEXT,
    lang         TEXT,
    modified     TEXT,               -- timestamp reported by the source
    content_hash TEXT,
    path         TEXT,               -- raw file in data/raw
    status       TEXT NOT NULL,      -- active | duplicate | deleted | error
    duplicate_of TEXT,
    fetched_at   TEXT NOT NULL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    new INTEGER, updated INTEGER, unchanged INTEGER, duplicates INTEGER, deleted INTEGER, errors INTEGER
);
"""


@dataclass
class StoredDoc:
    doc_id: str
    modified: str | None
    content_hash: str | None
    status: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifest:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)

    def get(self, doc_id: str) -> StoredDoc | None:
        row = self.conn.execute(
            "SELECT doc_id, modified, content_hash, status FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return StoredDoc(*row) if row else None

    def find_by_hash(self, content_hash: str, exclude_doc_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT doc_id FROM documents WHERE content_hash = ? AND status = 'active' AND doc_id != ?",
            (content_hash, exclude_doc_id),
        ).fetchone()
        return row[0] if row else None

    def upsert(self, **fields) -> None:
        fields.setdefault("fetched_at", now_iso())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{c} = excluded.{c}" for c in fields if c != "doc_id")
        self.conn.execute(
            f"INSERT INTO documents ({cols}) VALUES ({marks}) ON CONFLICT(doc_id) DO UPDATE SET {updates}",
            tuple(fields.values()),
        )
        self.conn.commit()

    def active_ids(self, site: str, doc_type: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT doc_id FROM documents WHERE site = ? AND doc_type = ? AND status = 'active'", (site, doc_type)
        )
        return {r[0] for r in rows}

    def mark_deleted(self, doc_id: str) -> None:
        self.conn.execute("UPDATE documents SET status = 'deleted', fetched_at = ? WHERE doc_id = ?", (now_iso(), doc_id))
        self.conn.commit()

    def active_documents(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT doc_id, site, doc_type, url, title, lang, modified, path FROM documents WHERE status = 'active'"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur]

    def log_run(self, started_at: str, stats: dict) -> None:
        self.conn.execute(
            "INSERT INTO runs (started_at, finished_at, new, updated, unchanged, duplicates, deleted, errors)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (started_at, now_iso(), stats["new"], stats["updated"], stats["unchanged"],
             stats["duplicates"], stats["deleted"], stats["errors"]),
        )
        self.conn.commit()
