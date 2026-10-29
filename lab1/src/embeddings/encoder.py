"""Sentence-transformers wrapper that knows each model's query/passage prefixes."""
from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np

log = logging.getLogger(__name__)

# E5 models were trained with these prefixes; without them quality drops noticeably
PREFIXES = {
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
    "intfloat/multilingual-e5-base": ("query: ", "passage: "),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
}


@lru_cache(maxsize=4)
def _load(model_name: str, device: str):
    from sentence_transformers import SentenceTransformer
    log.info("Loading embedding model %s", model_name)
    return SentenceTransformer(model_name, device=device)


class Encoder:
    def __init__(self, model_name: str, device: str = "cpu", batch_size: int = 32):
        self.model_name = model_name
        self.model = _load(model_name, device)
        self.batch_size = batch_size
        self.query_prefix, self.passage_prefix = PREFIXES.get(model_name, ("", ""))

    @property
    def dimension(self) -> int:
        return self.model.get_sentence_embedding_dimension()

    def _encode(self, texts: list[str], show_progress: bool) -> np.ndarray:
        return self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                 show_progress_bar=show_progress, convert_to_numpy=True)

    def encode_passages(self, texts: list[str], show_progress: bool = True) -> np.ndarray:
        return self._encode([self.passage_prefix + t for t in texts], show_progress)

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode([self.query_prefix + text], False)[0]

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode([self.query_prefix + t for t in texts], False)
