"""Grabber orchestration: listing -> diff with the manifest -> fetch new/changed -> dedup -> save.

Run:  python -m src.cli grab
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ..config import resolve
from .http import HttpClient
from .sources import FetchedDoc, ListingItem, PdfSource, WordPressSource
from .state import Manifest, now_iso

log = logging.getLogger(__name__)

WP_TYPES = {"pages": "page", "posts": "post"}


def content_hash(doc: FetchedDoc) -> str:
    """Hash of the visible text, so markup-only edits do not count as changes."""
    text = BeautifulSoup(doc.body, "lxml").get_text(" ") if doc.body_format == "html" else doc.body
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Grabber:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        g = cfg["grabber"]
        self.client = HttpClient(g["user_agent"], g["timeout_s"], g["max_retries"], g["delay_s"])
        self.raw_dir = resolve(cfg["paths"]["raw_dir"])
        self.manifest = Manifest(resolve(cfg["paths"]["data_dir"]) / "manifest.sqlite")
        self.stats = dict(new=0, updated=0, unchanged=0, duplicates=0, deleted=0, errors=0)

    # --- helpers -------------------------------------------------------------
    def _needs_fetch(self, doc_id: str, item: ListingItem) -> bool:
        stored = self.manifest.get(doc_id)
        if stored is None or stored.status == "error":
            return True
        if stored.modified != item.modified:
            return True
        self.stats["unchanged"] += 1
        return False

    def _save(self, site: str, doc_type: str, doc: FetchedDoc, extra_bytes: bytes | None = None) -> None:
        doc_id = f"{site}:{doc_type}:{doc.source_id}"
        h = content_hash(doc)
        previous = self.manifest.get(doc_id)

        original = self.manifest.find_by_hash(h, exclude_doc_id=doc_id)
        if original:
            # same text already stored under another id (page copies, re-uploaded PDFs)
            self.manifest.upsert(doc_id=doc_id, site=site, doc_type=doc_type, url=doc.url, title=doc.title,
                                 lang=doc.lang, modified=doc.modified, content_hash=h, path=None,
                                 status="duplicate", duplicate_of=original, error=None)
            self.stats["duplicates"] += 1
            return

        if previous and previous.status == "active" and previous.content_hash == h:
            # timestamp moved but the text did not: refresh metadata only
            self.manifest.upsert(doc_id=doc_id, site=site, doc_type=doc_type, url=doc.url, title=doc.title,
                                 lang=doc.lang, modified=doc.modified, content_hash=h, status="active")
            self.stats["unchanged"] += 1
            return

        out_dir = self.raw_dir / site / doc_type
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{doc.source_id}.json"
        record = dict(doc_id=doc_id, site=site, doc_type=doc_type, url=doc.url, title=doc.title, lang=doc.lang,
                      modified=doc.modified, body_format=doc.body_format, body=doc.body)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if extra_bytes is not None:
            (out_dir / f"{doc.source_id}.pdf").write_bytes(extra_bytes)

        self.manifest.upsert(doc_id=doc_id, site=site, doc_type=doc_type, url=doc.url, title=doc.title,
                             lang=doc.lang, modified=doc.modified, content_hash=h,
                             path=str(path.relative_to(self.raw_dir)), status="active", duplicate_of=None, error=None)
        self.stats["updated" if previous and previous.status == "active" else "new"] += 1

    def _error(self, site: str, doc_type: str, item: ListingItem, exc: Exception) -> None:
        log.error("Failed %s %s: %s", doc_type, item.url, exc)
        self.manifest.upsert(doc_id=f"{site}:{doc_type}:{item.source_id}", site=site, doc_type=doc_type,
                             url=item.url, title=item.title, lang=None, modified=item.modified,
                             status="error", error=str(exc)[:500])
        self.stats["errors"] += 1

    # --- sources -------------------------------------------------------------
    def grab_wordpress(self, site: dict) -> None:
        wp = WordPressSource(self.client, site["base_url"], self.cfg["grabber"]["per_page"])
        for content_type, opts in site["content_types"].items():
            doc_type = WP_TYPES[content_type]
            try:
                listing = wp.list(content_type, opts.get("max_items"))
            except Exception as exc:  # whole listing failed: keep previous data untouched
                log.error("Listing %s/%s failed: %s", site["name"], content_type, exc)
                self.stats["errors"] += 1
                continue
            log.info("%s/%s: %d items in listing", site["name"], content_type, len(listing))

            to_fetch = {it.source_id: it for it in listing
                        if self._needs_fetch(f"{site['name']}:{doc_type}:{it.source_id}", it)}
            try:
                for doc in wp.fetch(content_type, list(to_fetch)):
                    self._save(site["name"], doc_type, doc)
                    to_fetch.pop(doc.source_id, None)
            except Exception as exc:
                log.error("Fetch %s/%s failed: %s", site["name"], content_type, exc)
            for item in to_fetch.values():  # anything not returned by the API
                self._error(site["name"], doc_type, item, RuntimeError("not returned by API"))

            # Pages are listed completely, so a missing page means it was removed from the site.
            # Posts are capped by max_items, so absence there only means "older than the cap".
            if opts.get("max_items") is None:
                listed = {f"{site['name']}:{doc_type}:{it.source_id}" for it in listing}
                for doc_id in self.manifest.active_ids(site["name"], doc_type) - listed:
                    self.manifest.mark_deleted(doc_id)
                    self.stats["deleted"] += 1

    def grab_pdfs(self, site: dict) -> None:
        p = self.cfg["grabber"]["pdf"]
        source = PdfSource(self.client, site["base_url"], p["include_keywords"], p["exclude_keywords"],
                           p["max_files_per_site"], p["max_size_mb"], self.cfg["grabber"]["per_page"])
        try:
            listing = source.list()
        except Exception as exc:
            log.error("PDF listing %s failed: %s", site["name"], exc)
            self.stats["errors"] += 1
            return
        log.info("%s/pdf: %d relevant PDFs", site["name"], len(listing))
        # the selection is "the newest N relevant PDFs": anything outside it now (removed from the
        # site, or excluded by changed keywords) leaves the corpus
        listed = {f"{site['name']}:pdf:{it.source_id}" for it in listing}
        for doc_id in self.manifest.active_ids(site["name"], "pdf") - listed:
            self.manifest.mark_deleted(doc_id)
            self.stats["deleted"] += 1
        for item in listing:
            if not self._needs_fetch(f"{site['name']}:pdf:{item.source_id}", item):
                continue
            try:
                doc, data = source.fetch(item)
                self._save(site["name"], "pdf", doc, extra_bytes=data)
            except Exception as exc:
                self._error(site["name"], "pdf", item, exc)

    def run(self) -> dict:
        started = now_iso()
        for site in self.cfg["grabber"]["sites"]:
            self.grab_wordpress(site)
            if self.cfg["grabber"]["pdf"]["enabled"]:
                self.grab_pdfs(site)
        self.manifest.log_run(started, self.stats)
        log.info("Grab finished: %s", self.stats)
        return self.stats
