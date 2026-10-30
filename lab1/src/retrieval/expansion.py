"""Small-to-big context expansion.

Chunks are small so that search is precise, but an answer can span a chunk border:
a list of 7 study fields was split 4 + 3 and the model only saw the last three.
Each selected chunk is therefore widened with its neighbours (chunk_index ± window)
from the same document; adjacent selections of one document are merged into a
single passage, so nothing is sent twice.
"""
from __future__ import annotations

from .vector_store import Hit, VectorStore


def _strip_prefix(text: str, title: str) -> str:
    """Heading chunks start with "title | section"; keep it only once per merged passage."""
    first, _, rest = text.partition("\n")
    return rest if title and first.startswith(title) and rest else text


def expand_with_neighbors(hits: list[Hit], store: VectorStore, window: int) -> list[Hit]:
    if window <= 0 or not hits:
        return hits

    # indices to include per document, plus the best score that pulled each document in
    wanted: dict[str, set[int]] = {}
    best: dict[str, Hit] = {}
    for h in hits:
        doc, idx = h.metadata["document_id"], int(h.metadata["chunk_index"])
        wanted.setdefault(doc, set()).update(range(max(idx - window, 0), idx + window + 1))
        if doc not in best or h.score > best[doc].score:
            best[doc] = h

    fetched = store.get([f"{doc}#{i}" for doc, idxs in wanted.items() for i in sorted(idxs)])

    passages: list[Hit] = []
    for doc, idxs in wanted.items():
        present = [i for i in sorted(idxs) if f"{doc}#{i}" in fetched]
        # contiguous runs become one passage each
        runs, run = [], []
        for i in present:
            if run and i != run[-1] + 1:
                runs.append(run)
                run = []
            run.append(i)
        if run:
            runs.append(run)
        title = best[doc].metadata.get("title", "")
        for run in runs:
            texts = [fetched[f"{doc}#{i}"][0] for i in run]
            text = "\n".join([texts[0]] + [_strip_prefix(t, title) for t in texts[1:]])
            meta = {**best[doc].metadata, "chunk_index": run[0], "chunks": f"{run[0]}-{run[-1]}"}
            # a run keeps the score of the selected chunk inside it (the document's best otherwise)
            inside = [h for h in hits if h.metadata["document_id"] == doc and int(h.metadata["chunk_index"]) in run]
            score = max(h.score for h in inside) if inside else best[doc].score
            passages.append(Hit(chunk_id=f"{doc}#{run[0]}-{run[-1]}", text=text, score=score, metadata=meta))

    return sorted(passages, key=lambda p: p.score, reverse=True)
