"""Raw documents from the grabber -> data/processed/documents.jsonl (clean text + metadata)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import resolve
from ..grabber.state import Manifest
from .cleaning import clean_document

log = logging.getLogger(__name__)


def process_documents(cfg: dict) -> Path:
    raw_dir = resolve(cfg["paths"]["raw_dir"])
    out_path = resolve(cfg["paths"]["processed_dir"]) / "documents.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(resolve(cfg["paths"]["data_dir"]) / "manifest.sqlite")
    pp = cfg["preprocessing"]

    kept = skipped = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for meta in sorted(manifest.active_documents(), key=lambda d: d["doc_id"]):
            raw = json.loads((raw_dir / meta["path"]).read_text(encoding="utf-8"))
            text = clean_document(raw["body"], raw["body_format"], pp["drop_selectors"])
            if len(text) < pp["min_doc_chars"]:
                skipped += 1  # empty landing pages, sliders, "test" pages
                continue
            record = {
                "document_id": meta["doc_id"],
                "source": meta["site"],
                "doc_type": meta["doc_type"],
                "url": meta["url"],
                "title": meta["title"],
                "lang": meta["lang"],
                "updated_at": meta["modified"],
                "text": text,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept += 1
    log.info("Processed documents: kept %d, skipped %d (shorter than %d chars)", kept, skipped, pp["min_doc_chars"])
    return out_path


def load_documents(cfg: dict) -> list[dict]:
    path = resolve(cfg["paths"]["processed_dir"]) / "documents.jsonl"
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]
