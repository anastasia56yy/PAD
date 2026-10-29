"""Data sources: WordPress REST API (pages, posts) and PDF documents from the media library.

Both utm.md and fcim.utm.md run WordPress, so /wp-json/wp/v2/ gives structured
data (id, link, title, modified, content) instead of crawling and parsing every
HTML page with its menus and sidebars.
"""
from __future__ import annotations

import html
import io
import logging
import re
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlparse

from pypdf import PdfReader

from .http import HttpClient

log = logging.getLogger(__name__)


@dataclass
class ListingItem:
    """Lightweight entry from a listing request: enough to decide whether to fetch the content."""
    source_id: int
    url: str
    title: str
    modified: str


@dataclass
class FetchedDoc:
    source_id: int
    url: str
    title: str
    modified: str
    lang: str
    body: str          # HTML for pages/posts, plain text for PDF
    body_format: str   # html | text


def detect_lang(url: str) -> str:
    """Polylang/WPML put the language in the first path segment: /en/..., /ru/...; default is Romanian."""
    first = urlparse(url).path.strip("/").split("/")[0]
    return first if first in {"en", "ru"} else "ro"


def clean_title(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


class WordPressSource:
    def __init__(self, client: HttpClient, base_url: str, per_page: int = 100):
        self.client = client
        self.api = base_url.rstrip("/") + "/wp-json/wp/v2"
        self.per_page = per_page

    def _paginate(self, endpoint: str, params: dict, max_items: int | None) -> Iterator[dict]:
        page, seen = 1, 0
        while True:
            resp = self.client.get(f"{self.api}/{endpoint}", params={**params, "per_page": self.per_page, "page": page})
            items = resp.json()
            for item in items:
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
            total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
            if page >= total_pages or not items:
                return
            page += 1

    def list(self, content_type: str, max_items: int | None = None) -> list[ListingItem]:
        params = {"orderby": "modified", "order": "desc", "_fields": "id,link,title,modified_gmt"}
        return [
            ListingItem(it["id"], it["link"], clean_title(it["title"]["rendered"]), it["modified_gmt"])
            for it in self._paginate(content_type, params, max_items)
        ]

    def fetch(self, content_type: str, ids: list[int]) -> Iterator[FetchedDoc]:
        """Full content only for the ids that are new or changed (batches of `per_page`)."""
        for start in range(0, len(ids), self.per_page):
            batch = ids[start:start + self.per_page]
            params = {"include": ",".join(map(str, batch)), "_fields": "id,link,title,modified_gmt,content",
                      "per_page": len(batch)}
            # status=publish is the default, so drafts/private pages never come back
            for it in self.client.get(f"{self.api}/{content_type}", params=params).json():
                yield FetchedDoc(
                    source_id=it["id"], url=it["link"], title=clean_title(it["title"]["rendered"]),
                    modified=it["modified_gmt"], lang=detect_lang(it["link"]),
                    body=it["content"]["rendered"], body_format="html",
                )


class PdfSource:
    """Normative PDF documents (regulations, guides) from the WordPress media library."""

    def __init__(self, client: HttpClient, base_url: str, include: list[str], exclude: list[str],
                 max_files: int, max_size_mb: float, per_page: int = 100):
        self.wp = WordPressSource(client, base_url, per_page)
        self.client = client
        self.include = [k.lower() for k in include]
        self.exclude = [k.lower() for k in exclude]
        self.max_files = max_files
        self.max_bytes = int(max_size_mb * 1024 * 1024)

    def is_relevant(self, url: str, title: str) -> bool:
        name = (urlparse(url).path.rsplit("/", 1)[-1] + " " + title).lower()
        if any(k in name for k in self.exclude):
            return False
        return any(k in name for k in self.include)

    def list(self) -> list[ListingItem]:
        params = {"mime_type": "application/pdf", "orderby": "modified", "order": "desc",
                  "_fields": "id,source_url,title,modified_gmt"}
        selected = []
        for it in self.wp._paginate("media", params, max_items=None):
            item = ListingItem(it["id"], it["source_url"], clean_title(it["title"]["rendered"]), it["modified_gmt"])
            if self.is_relevant(item.url, item.title):
                selected.append(item)
                if len(selected) >= self.max_files:
                    break
        return selected

    def fetch(self, item: ListingItem) -> tuple[FetchedDoc, bytes]:
        resp = self.client.get(item.url, stream=True)
        size = int(resp.headers.get("Content-Length") or 0)
        if size > self.max_bytes:
            raise ValueError(f"PDF too large: {size / 1e6:.1f} MB")
        data = resp.content
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        if not text:
            raise ValueError("PDF has no text layer (scanned document)")
        doc = FetchedDoc(item.source_id, item.url, item.title, item.modified, detect_lang(item.url), text, "text")
        return doc, data
