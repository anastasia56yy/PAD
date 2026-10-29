"""Web interface for the RAG pipeline: a JSON API plus one static page.

    python -m app.server            # http://127.0.0.1:8000
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src.config import load_config, resolve  # noqa: E402
from src.pipeline import RAGPipeline, detect_question_lang  # noqa: E402

log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="UTM RAG Assistant", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_cfg = load_config()
_pipeline: RAGPipeline | None = None
_lock = threading.Lock()  # models and Chroma client are shared: one question at a time


def pipeline() -> RAGPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = RAGPipeline(_cfg)
    return _pipeline


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
def info() -> dict:
    docs_file = resolve(_cfg["paths"]["processed_dir"]) / "documents.jsonl"
    documents = sum(1 for _ in open(docs_file, encoding="utf-8")) if docs_file.exists() else 0
    return {
        "documents": documents,
        "sources": [s["base_url"].replace("https://", "") for s in _cfg["grabber"]["sites"]],
        "llm": _cfg["generation"]["model"],
        "embeddings": _cfg["embeddings"]["model"].split("/")[-1],
    }


@app.post("/api/ask")
def ask(body: Question) -> dict:
    question = body.question.strip()
    try:
        with _lock:
            result = pipeline().answer(question)
    except RuntimeError as exc:  # Ollama not running / model not pulled / empty index
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    t = result.trace
    sources = []
    for i, h in enumerate(result.sources, 1):
        m = h.metadata
        sources.append({
            "n": i, "title": m.get("title"), "url": m.get("url"), "section": m.get("section") or "",
            "site": m.get("source"), "type": m.get("doc_type"), "lang": m.get("lang"),
            "score": round(h.score, 2), "text": h.text,
        })
    return {
        "question": question,
        "lang": detect_question_lang(question),
        "answer": result.answer,
        "refused": result.refused,
        "sources": sources,
        "stats": {
            "retrieved": t.get("retrieved", 0),
            "filtered": t.get("after_per_doc", 0),
            "context": t.get("to_llm", 0),
            "total_s": round(t.get("total_s", 0.0), 1),
            "translated": bool(t.get("translated")),
        },
    }


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("huggingface_hub", "httpx", "chromadb", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info("Loading models...")
    pipeline()  # load models before the first request, not during it
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), log_level="warning")


if __name__ == "__main__":
    main()
