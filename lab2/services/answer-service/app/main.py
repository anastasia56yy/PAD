"""answer-service: turns a question into an answer.

question --REST--> search-service (context) --> prompt --> Ollama (LLM) --> answer
Owns: no database (stateless, so it can be scaled to several replicas freely).
Publishes: question.answered (history-service and notification-service react to it).
"""
from __future__ import annotations

import logging
import time

from fastapi import Header
from pydantic import BaseModel, Field

from utm_common import events
from utm_common.config import env, env_float
from utm_common.http import call
from utm_common.logging import log_event
from utm_common.rag import hit_from_dict, rag_config
from utm_common.web import ServiceError, create_app

log = logging.getLogger("answer")
SEARCH_URL = env("SEARCH_URL", "http://search-service:8000")
LLM_TIMEOUT = env_float("LLM_TIMEOUT_SECONDS", 120)

cfg = rag_config()


def check_ollama() -> None:
    import httpx  # noqa: PLC0415

    httpx.get(cfg["generation"]["host"].rstrip("/") + "/api/tags", timeout=3).raise_for_status()


app = create_app("answer-service", {"ollama": check_ollama},
                 description="Answer generation over the context returned by search-service.")


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def generate(question: str, hits: list) -> tuple[str, list, dict]:
    """The generation half of the lab1 pipeline (prompt v3, translation fallback, citation check)."""
    from src.generation.llm import OllamaLLM  # noqa: PLC0415
    from src.generation.prompts import LANGUAGE_NAMES, TRANSLATE, build_messages  # noqa: PLC0415
    from src.pipeline import (detect_question_lang, detect_text_lang, is_refusal,  # noqa: PLC0415
                              sanitize_citations, strip_trailing_refusal)

    g = cfg["generation"]
    llm = OllamaLLM(g["model"], g["host"], g["temperature"], g["num_ctx"], timeout_s=LLM_TIMEOUT)
    lang = detect_question_lang(question)
    messages, used = build_messages(g["prompt"], question, hits, g["max_context_chars"], lang)
    try:
        answer, gen_s = llm.chat(messages)
        translated = False
        if g["prompt"] in ("strict_v2", "strict_v3") and not is_refusal(answer) and detect_text_lang(answer) != lang:
            answer, extra = llm.chat([{"role": "user", "content": TRANSLATE.format(
                language=LANGUAGE_NAMES[lang], answer=answer)}])
            gen_s += extra
            translated = True
    except RuntimeError as exc:  # Ollama down or model missing: a clear 503, not a stack trace
        raise ServiceError(503, "llm_unavailable", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - e.g. read timeout of a long generation
        raise ServiceError(504, "llm_timeout", "The language model did not answer in time") from exc
    answer = strip_trailing_refusal(answer)
    answer, bad = sanitize_citations(answer, len(used))
    return answer, used, {"generation_s": round(gen_s, 2), "translated": translated, "invalid_citations": bad}


@app.post("/ask", tags=["answer"], summary="Answer a question from the UTM/FCIM documents")
def ask(body: AskIn, x_user_id: str | None = Header(default=None)) -> dict:
    from src.generation.prompts import REFUSALS  # noqa: PLC0415
    from src.pipeline import detect_question_lang, is_refusal  # noqa: PLC0415

    start = time.perf_counter()
    question = body.question.strip()
    lang = detect_question_lang(question)

    resp = call("POST", f"{SEARCH_URL}/search", json={"question": question}, timeout=30)
    if resp.status_code != 200:
        raise ServiceError(503, "search_unavailable", resp.json().get("error", {}).get("message", "Search failed"))
    found = resp.json()
    hits = [hit_from_dict(h) for h in found["hits"]]
    trace = found["trace"]

    if not hits:
        answer, used, gen = REFUSALS[lang], [], {"generation_s": 0.0, "translated": False}
    else:
        answer, used, gen = generate(question, hits)
    refused = not used or is_refusal(answer)

    sources = [{"n": i, "title": h.metadata.get("title"), "url": h.metadata.get("url"),
                "section": h.metadata.get("section") or "", "site": h.metadata.get("source"),
                "type": h.metadata.get("doc_type"), "score": round(h.score, 2), "text": h.text}
               for i, h in enumerate(used, 1)]
    related, seen = [], set()
    if refused:
        for r in found.get("related", []):
            url = r["metadata"].get("url")
            if url not in seen:
                seen.add(url)
                related.append({"title": r["metadata"].get("title"), "url": url})

    total = round(time.perf_counter() - start, 2)
    result = {
        "question": question, "lang": lang, "answer": answer, "refused": refused,
        "confidence": found.get("confidence", "high"), "related": related, "sources": sources,
        "stats": {"retrieved": trace.get("retrieved", 0), "filtered": trace.get("after_per_doc", 0),
                  "context": len(used), "total_s": total, "translated": gen["translated"]},
    }
    events.publish("question.answered", {
        "user_id": int(x_user_id) if x_user_id else None, "question": question, "answer": answer,
        "refused": refused, "confidence": result["confidence"], "latency_s": total,
        "sources": [{"title": s["title"], "url": s["url"]} for s in sources],
    })
    log_event(log, "Question answered", refused=refused, context=len(used), latency_s=total)
    return result
