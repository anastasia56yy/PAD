"""processed documents -> chunks -> embeddings -> Chroma collection."""
from __future__ import annotations

import hashlib
import logging

from .embeddings.encoder import Encoder
from .preprocessing.chunking import chunk_document
from .preprocessing.process import load_documents, process_documents
from .retrieval.vector_store import VectorStore

log = logging.getLogger(__name__)


def corpus_fingerprint(docs: list[dict]) -> str:
    h = hashlib.sha256()
    for d in docs:
        h.update(d["document_id"].encode())
        h.update(hashlib.sha256(d["text"].encode()).digest())
    return h.hexdigest()[:16]


def build_chunks(cfg: dict, docs: list[dict]) -> tuple[list[str], list[str], list[dict]]:
    ch = cfg["chunking"]
    ids, texts, metas = [], [], []
    for doc in docs:
        for chunk in chunk_document(doc["text"], doc["title"], ch["strategy"], ch["chunk_size"],
                                    ch["chunk_overlap"], ch["min_chunk_chars"]):
            ids.append(f"{doc['document_id']}#{chunk.index}")
            texts.append(chunk.text)
            metas.append({
                "document_id": doc["document_id"], "source": doc["source"], "doc_type": doc["doc_type"],
                "url": doc["url"], "title": doc["title"], "lang": doc["lang"],
                "updated_at": doc["updated_at"] or "", "section": chunk.section, "chunk_index": chunk.index,
            })
    return ids, texts, metas


def build_index(cfg: dict, reprocess: bool = True, force: bool = False) -> dict:
    if reprocess:
        process_documents(cfg)
    docs = load_documents(cfg)
    fingerprint = corpus_fingerprint(docs)
    store = VectorStore(cfg)

    if not force and store.fingerprint() == fingerprint and store.count() > 0:
        log.info("Index %s is up to date (%d chunks)", store.name, store.count())
        return {"collection": store.name, "documents": len(docs), "chunks": store.count(), "rebuilt": False}

    ids, texts, metas = build_chunks(cfg, docs)
    encoder = Encoder(cfg["embeddings"]["model"], cfg["embeddings"]["device"], cfg["embeddings"]["batch_size"])
    log.info("Embedding %d chunks with %s (dim=%d)", len(texts), encoder.model_name, encoder.dimension)
    embeddings = encoder.encode_passages(texts)
    store.reset(fingerprint, cfg["vector_store"]["distance"])
    store.add(ids, texts, embeddings, metas)
    lengths = sorted(len(t) for t in texts)
    return {"collection": store.name, "documents": len(docs), "chunks": len(texts), "rebuilt": True,
            "dim": encoder.dimension, "median_chunk_chars": lengths[len(lengths) // 2] if lengths else 0}
