"""Context filters applied between vector search and the reranker/LLM."""
from __future__ import annotations

import re

from .vector_store import Hit


def by_score(hits: list[Hit], threshold: float | None) -> list[Hit]:
    return hits if threshold is None else [h for h in hits if h.score >= threshold]


def by_length(hits: list[Hit], min_chars: int | None) -> list[Hit]:
    return hits if not min_chars else [h for h in hits if len(h.text) >= min_chars]


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(len(words) - n + 1, 1))}


def jaccard(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def deduplicate(hits: list[Hit], threshold: float | None) -> list[Hit]:
    """Drop chunks whose word-3-gram Jaccard similarity to an already kept (higher scored) chunk
    is above the threshold: the same announcement posted on utm.md and fcim.utm.md, overlap windows, etc."""
    if threshold is None:
        return hits
    kept: list[Hit] = []
    for hit in hits:
        if all(jaccard(hit.text, k.text) < threshold for k in kept):
            kept.append(hit)
    return kept


def max_per_document(hits: list[Hit], limit: int | None) -> list[Hit]:
    """Keep at most `limit` chunks per document so one long regulation does not fill the whole context."""
    if not limit:
        return hits
    counts: dict[str, int] = {}
    kept = []
    for hit in hits:
        doc = hit.metadata["document_id"]
        if counts.get(doc, 0) < limit:
            kept.append(hit)
            counts[doc] = counts.get(doc, 0) + 1
    return kept


def metadata_where(languages: list[str] | None = None, sources: list[str] | None = None) -> dict | None:
    """Chroma `where` clause; applied inside the vector search, not after it."""
    clauses = []
    if languages:
        clauses.append({"lang": {"$in": languages}})
    if sources:
        clauses.append({"source": {"$in": sources}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def apply_filters(hits: list[Hit], cfg: dict) -> tuple[list[Hit], dict]:
    """Returns the kept hits and how many were left after each step (for logging/analysis)."""
    f = cfg["filters"]
    trace = {"retrieved": len(hits)}
    hits = by_score(hits, f.get("score_threshold"))
    trace["after_threshold"] = len(hits)
    hits = by_length(hits, f.get("min_chunk_chars"))
    trace["after_length"] = len(hits)
    hits = deduplicate(hits, f.get("dedup_threshold"))
    trace["after_dedup"] = len(hits)
    hits = max_per_document(hits, f.get("max_per_document"))
    trace["after_per_doc"] = len(hits)
    return hits, trace
