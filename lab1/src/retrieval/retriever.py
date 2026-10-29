"""Dense retriever: query -> embedding -> top-K chunks from Chroma."""
from __future__ import annotations

from ..embeddings.encoder import Encoder
from .filters import metadata_where
from .vector_store import Hit, VectorStore


class Retriever:
    def __init__(self, cfg: dict):
        emb = cfg["embeddings"]
        self.encoder = Encoder(emb["model"], emb["device"], emb["batch_size"])
        self.store = VectorStore(cfg)
        if self.store.count() == 0:
            raise RuntimeError(f"Collection {self.store.name} is empty, run `python -m src.cli index` first")
        self.top_k = cfg["retrieval"]["top_k"]
        self.where = metadata_where(cfg["filters"].get("languages"))

    def retrieve(self, query: str, k: int | None = None) -> list[Hit]:
        return self.store.search(self.encoder.encode_query(query), k or self.top_k, self.where)
