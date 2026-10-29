"""Retrieval and answer metrics.

Relevance is judged at the document level: a question lists the ids of the
documents that contain the answer (`relevant_docs`, including its translations
on the ro/en/ru versions of the site). A ranked list of chunks is turned into a
ranked list of unique documents before computing the metrics, so two chunks of
the same page do not count twice.
"""
from __future__ import annotations

import re
import unicodedata


def ranked_docs(hits) -> list[str]:
    seen, out = set(), []
    for h in hits:
        d = h.metadata.get("document_id", "")
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def hit_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return float(any(u in relevant for u in ranked[:k]))


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    top = ranked[:k]
    return len([u for u in top if u in relevant]) / len(top) if top else 0.0


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for i, u in enumerate(ranked, 1):
        if u in relevant:
            return 1.0 / i
    return 0.0


def _fold(text: str) -> str:
    """Lowercase, drop diacritics ('înscriere' = 'inscriere') and digit grouping ('5,140' = '5140')."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"(?<=\d)[ ,. ](?=\d{3}\b)", "", text)


def keyword_present(text: str, keyword: str) -> bool:
    """`keyword` may list alternatives: "4|four|patru"."""
    folded = _fold(text)
    return any(_fold(alt) in folded for alt in keyword.split("|"))


def keyword_recall(answer: str, keywords: list[str]) -> float:
    """Share of the expected key facts (numbers, names, terms) present in the answer."""
    if not keywords:
        return 0.0
    return sum(1 for k in keywords if keyword_present(answer, k)) / len(keywords)


def has_citation(answer: str) -> bool:
    return bool(re.search(r"\[\d+\]", answer))
