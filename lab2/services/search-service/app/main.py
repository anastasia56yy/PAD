"""search-service: retrieval + filters + reranker + context expansion over the Chroma index.

Owns: the vector index (Chroma, volume /data/index) — no other service reads it.
Consumes: documents.updated -> downloads the processed documents from ingestion-service and rebuilds
          its index in the background, keeping the old one for queries until the new one is ready.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

from utm_common import events
from utm_common.config import env
from utm_common.http import call
from utm_common.logging import log_event
from utm_common.rag import hit_to_dict, rag_config
from utm_common.web import ServiceError, create_app

log = logging.getLogger("search")
INGESTION_URL = env("INGESTION_URL", "http://ingestion-service:8000")

state = {"pipeline": None, "reindexing": False, "last_reindex": None, "error": None}
lock = threading.Lock()  # the embedding model, reranker and Chroma client are shared

DATA_DIR = Path(env("RAG_DATA_DIR", "/data"))
POINTER = DATA_DIR / "CURRENT_INDEX"  # name of the live index directory (blue-green switch)


def live_index_dir() -> Path:
    return DATA_DIR / (POINTER.read_text().strip() if POINTER.exists() else "index")


def load_pipeline(index_dir: Path | None = None) -> None:
    from src.pipeline import RAGPipeline  # noqa: PLC0415

    cfg = rag_config(**{"paths.index_dir": str(index_dir or live_index_dir())})
    state["pipeline"] = RAGPipeline(cfg, with_generation=False)
    log_event(log, "Pipeline loaded", index_dir=str(index_dir or live_index_dir()),
              chunks=state["pipeline"].retriever.store.count())


def check_index() -> None:
    if state["pipeline"] is None:
        raise RuntimeError(state["error"] or "index is loading")


app = create_app("search-service", {"index": check_index},
                 description="Semantic search over UTM/FCIM documents: retriever, filters, reranker.")


@app.on_event("startup")
def startup() -> None:
    def load() -> None:  # models take ~20 s to load: do not block the HTTP server (liveness must answer)
        try:
            load_pipeline()
        except Exception as exc:  # noqa: BLE001
            state["error"] = str(exc)
            log.exception("Pipeline failed to load")
    threading.Thread(target=load, daemon=True).start()
    if env("EVENTS_ENABLED", "true") == "true":
        events.start_consumer("search-service.documents-updated", ["documents.updated"], on_documents_updated)


def on_documents_updated(event: dict) -> None:
    if state["reindexing"]:
        return
    threading.Thread(target=reindex, args=(event["payload"],), daemon=True).start()


def reindex(payload: dict) -> None:
    """Blue-green: build the new index in a fresh directory while queries keep using the live one,
    then switch the pointer and the in-memory pipeline at once, and delete the old directory."""
    from src.indexing import build_index  # noqa: PLC0415

    state["reindexing"] = True
    started = time.perf_counter()
    new_dir = DATA_DIR / f"index-{int(time.time())}"
    try:
        resp = call("GET", f"{INGESTION_URL}/documents/export", timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"export failed: HTTP {resp.status_code}")
        staging = DATA_DIR / "processed-staging"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "documents.jsonl").write_bytes(resp.content)
        cfg = rag_config(**{"paths.index_dir": str(new_dir), "paths.processed_dir": str(staging)})
        info = build_index(cfg, reprocess=False, force=True)
        old_dir = live_index_dir()
        with lock:
            load_pipeline(new_dir)
            POINTER.write_text(new_dir.name)
            shutil.copyfile(staging / "documents.jsonl", DATA_DIR / "processed" / "documents.jsonl")
        shutil.rmtree(old_dir, ignore_errors=True)
        state["last_reindex"] = {**info, "seconds": round(time.perf_counter() - started, 1)}
        log_event(log, "Index rebuilt and switched", **{k: v for k, v in info.items() if k != "collection"})
    except Exception:  # noqa: BLE001
        shutil.rmtree(new_dir, ignore_errors=True)
        log.exception("Re-indexing failed, keeping the previous index")
    finally:
        state["reindexing"] = False


class SearchIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


@app.post("/search", tags=["search"], summary="Context for a question: ranked, filtered, expanded chunks")
def search(body: SearchIn) -> dict:
    pipe = state["pipeline"]
    if pipe is None:
        raise ServiceError(503, "index_loading", "Search index is still loading")
    with lock:
        hits, retrieved, trace = pipe.retrieve_context(body.question.strip())
    related = trace.pop("ranked", [])[:3]
    return {
        "hits": [hit_to_dict(h) for h in hits],
        "related": [hit_to_dict(h) for h in related],
        "confidence": trace.get("confidence", "high"),
        "trace": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in trace.items()},
    }


@app.get("/info", tags=["search"])
def info() -> dict:
    pipe = state["pipeline"]
    cfg = rag_config()
    docs_file = str(DATA_DIR / "processed" / "documents.jsonl")
    try:
        with open(docs_file, encoding="utf-8") as f:
            documents = sum(1 for _ in f)
    except OSError:
        documents = None
    return {
        "documents": documents,
        "chunks": pipe.retriever.store.count() if pipe else None,
        "embeddings": cfg["embeddings"]["model"].split("/")[-1],
        "llm": cfg["generation"]["model"],
        "sources": [s["base_url"].replace("https://", "") for s in cfg["grabber"]["sites"]],
        "reindexing": state["reindexing"],
        "last_reindex": state["last_reindex"],
    }
