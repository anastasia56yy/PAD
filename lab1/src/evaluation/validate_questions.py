"""Sanity check of the evaluation set against the processed corpus.

For every answerable question: each relevant document must exist, and every
keyword must occur in at least one of them. It also prints other documents that
contain all the keywords, as candidates that may be missing from relevant_docs.

    python -m src.evaluation.validate_questions
"""
from __future__ import annotations

import sys

from ..config import load_config
from ..preprocessing.process import load_documents
from .metrics import keyword_present
from .run_eval import load_questions


def validate(cfg: dict) -> int:
    docs = {d["document_id"]: d for d in load_documents(cfg)}
    problems = 0
    for q in load_questions(cfg):
        if q["type"] == "unanswerable":
            continue
        missing = [d for d in q["relevant_docs"] if d not in docs]
        texts = [docs[d]["text"] for d in q["relevant_docs"] if d in docs]
        absent = [k for k in q["keywords"] if not any(keyword_present(t, k) for t in texts)]
        candidates = [d for d, doc in docs.items() if d not in q["relevant_docs"]
                      and q["keywords"] and all(keyword_present(doc["text"], k) for k in q["keywords"])]
        if missing or absent:
            problems += 1
            print(f"{q['id']}: missing docs {missing}, keywords not found {absent}")
        if candidates and len(candidates) <= 5:
            print(f"{q['id']}: other docs containing all keywords: {candidates}")
    print(f"{problems} problem(s)")
    return problems


if __name__ == "__main__":
    sys.exit(1 if validate(load_config()) else 0)
