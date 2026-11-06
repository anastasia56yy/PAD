"""Bridge to the lab1 RAG code, which is copied into the images as the package `src` under RAG_ROOT.

The lab1 configuration (configs/default.yaml) is the single source of the tuned pipeline settings;
services override only what depends on the environment: paths, Ollama host, device.
"""
from __future__ import annotations

import os
import sys

from .config import env

RAG_ROOT = env("RAG_ROOT", "/app/rag")
if RAG_ROOT not in sys.path:
    sys.path.insert(0, RAG_ROOT)


def rag_config(**extra) -> dict:
    from src.config import load_config  # noqa: PLC0415 - path set above

    data = env("RAG_DATA_DIR", os.path.join(RAG_ROOT, "data"))
    overrides = {
        "paths.data_dir": data,
        "paths.raw_dir": os.path.join(data, "raw"),
        "paths.processed_dir": os.path.join(data, "processed"),
        "paths.index_dir": os.path.join(data, "index"),
        "embeddings.device": env("EMBEDDINGS_DEVICE", "cpu"),
        "generation.host": env("OLLAMA_HOST", "http://host.docker.internal:11434"),
        "generation.model": env("OLLAMA_MODEL", "qwen2.5:3b"),
    }
    overrides.update(extra)
    return load_config(overrides=overrides)


def hit_to_dict(h) -> dict:
    return {"chunk_id": h.chunk_id, "text": h.text, "score": round(float(h.score), 4), "metadata": h.metadata}


def hit_from_dict(d: dict):
    from src.retrieval.vector_store import Hit  # noqa: PLC0415

    return Hit(chunk_id=d["chunk_id"], text=d["text"], score=d["score"], metadata=d["metadata"])
