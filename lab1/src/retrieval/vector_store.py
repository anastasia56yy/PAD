"""Chroma persistent vector store.

Chroma was chosen over FAISS because it stores metadata next to the vectors and
can filter by it (language, source, document type) inside the query, it persists
to disk without extra code, and it runs embedded with no separate server. For a
corpus of a few thousand chunks the HNSW index is more than fast enough.

One collection per (chunking strategy, size, overlap, embedding model), so the
experiments can switch between indexes without rebuilding them each time.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import chromadb
import numpy as np

from ..config import resolve


@dataclass
class Hit:
    chunk_id: str
    text: str
    score: float          # cosine similarity in [-1, 1]
    metadata: dict


def collection_name(cfg: dict) -> str:
    ch, emb = cfg["chunking"], cfg["embeddings"]
    key = f"{ch['strategy']}|{ch['chunk_size']}|{ch['chunk_overlap']}|{ch['min_chunk_chars']}|{emb['model']}"
    digest = hashlib.md5(key.encode()).hexdigest()[:10]
    model_short = emb["model"].split("/")[-1].replace(".", "-")[:30]
    return f"{ch['strategy']}-{ch['chunk_size']}-{model_short}-{digest}"


class VectorStore:
    def __init__(self, cfg: dict):
        path = resolve(cfg["paths"]["index_dir"])
        path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path))
        self.name = collection_name(cfg)
        self.collection = self.client.get_or_create_collection(
            self.name, metadata={"hnsw:space": cfg["vector_store"]["distance"]}
        )

    def count(self) -> int:
        return self.collection.count()

    def fingerprint(self) -> str | None:
        return (self.collection.metadata or {}).get("corpus_fingerprint")

    def reset(self, corpus_fingerprint: str, distance: str) -> None:
        self.client.delete_collection(self.name)
        self.collection = self.client.create_collection(
            self.name, metadata={"hnsw:space": distance, "corpus_fingerprint": corpus_fingerprint}
        )

    def add(self, ids: list[str], texts: list[str], embeddings: np.ndarray, metadatas: list[dict],
            batch: int = 1000) -> None:
        for i in range(0, len(ids), batch):
            self.collection.add(ids=ids[i:i + batch], documents=texts[i:i + batch],
                                embeddings=embeddings[i:i + batch].tolist(), metadatas=metadatas[i:i + batch])

    def search(self, query_embedding: np.ndarray, k: int, where: dict | None = None) -> list[Hit]:
        res = self.collection.query(query_embeddings=[query_embedding.tolist()], n_results=k, where=where,
                                    include=["documents", "metadatas", "distances"])
        return [
            Hit(chunk_id=cid, text=doc, score=1.0 - dist, metadata=meta)
            for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]
