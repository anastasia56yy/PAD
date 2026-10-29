"""Cross-encoder reranker.

The bi-encoder embeds query and chunk separately, so it only compares two
vectors. A cross-encoder reads the (query, chunk) pair together and scores real
relevance, which is more accurate but too slow to run over the whole index:
hence retrieve top-20 cheaply, rerank them, pass the best 5 to the LLM.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from functools import lru_cache

from ..embeddings.encoder import resolve_device
from ..retrieval.vector_store import Hit

log = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def _load(model_name: str, device: str):
    from sentence_transformers import CrossEncoder
    device = resolve_device(device)
    log.info("Loading reranker %s on %s", model_name, device)
    return CrossEncoder(model_name, max_length=512, device=device)


class Reranker:
    def __init__(self, model_name: str, top_n: int = 5, min_score: float | None = None, device: str = "auto"):
        self.model = _load(model_name, device)
        self.top_n = top_n
        self.min_score = min_score

    def rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        if not hits:
            return []
        scores = self.model.predict([(query, h.text) for h in hits], show_progress_bar=False)
        ranked = sorted(
            (replace(h, metadata={**h.metadata, "retrieval_score": h.score}, score=float(s)) for h, s in zip(hits, scores)),
            key=lambda h: h.score, reverse=True,
        )
        if self.min_score is not None:
            ranked = [h for h in ranked if h.score >= self.min_score]
        return ranked[:self.top_n]
