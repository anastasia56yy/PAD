"""The full RAG pipeline: retrieve -> filter -> rerank -> generate (with refusal when context is missing)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .generation.llm import OllamaLLM
from .generation.prompts import REFUSALS, build_messages
from .reranking.reranker import Reranker
from .retrieval.filters import apply_filters
from .retrieval.retriever import Retriever
from .retrieval.vector_store import Hit

RO_HINTS = re.compile(r"[ăâîșțşţ]|\b(care|este|sunt|cum|unde|când|cine|ce|pentru|și|la|de)\b", re.IGNORECASE)


def detect_question_lang(question: str) -> str:
    if re.search(r"[а-яё]", question, re.IGNORECASE):
        return "ru"
    return "ro" if RO_HINTS.search(question) else "en"


def is_refusal(answer: str) -> bool:
    low = answer.lower()
    return any(r.lower().rstrip(".") in low for r in REFUSALS.values())


@dataclass
class RAGResult:
    question: str
    answer: str
    refused: bool
    sources: list[Hit]
    retrieved: list[Hit] = field(default_factory=list)   # raw top-K before filters
    trace: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        def hit(h: Hit) -> dict:
            return {"id": h.chunk_id, "score": round(h.score, 4), "url": h.metadata.get("url"),
                    "title": h.metadata.get("title"), "section": h.metadata.get("section"), "text": h.text}
        return {"question": self.question, "answer": self.answer, "refused": self.refused,
                "sources": [hit(h) for h in self.sources], "trace": self.trace}

    def format(self) -> str:
        lines = [f"Ответ:\n{self.answer}", "", "Источники:"]
        seen = set()
        for i, h in enumerate(self.sources, 1):
            url = h.metadata.get("url")
            if url not in seen:
                seen.add(url)
                lines.append(f"[{i}] {h.metadata.get('title')} — {url}")
        if not self.sources:
            lines.append("- (нет релевантных источников)")
        t = self.trace
        lines.append(f"\n(retrieved {t.get('retrieved')}, after filters {t.get('after_per_doc')}, "
                     f"to LLM {t.get('to_llm')}, {t.get('total_s', 0):.1f}s)")
        return "\n".join(lines)


class RAGPipeline:
    def __init__(self, cfg: dict, with_generation: bool = True):
        self.cfg = cfg
        self.retriever = Retriever(cfg)
        rr = cfg["reranker"]
        self.reranker = (Reranker(rr["model"], rr["top_n"], rr.get("min_score"), cfg["embeddings"]["device"])
                         if rr["enabled"] else None)
        g = cfg["generation"]
        self.llm = OllamaLLM(g["model"], g["host"], g["temperature"], g["num_ctx"]) if with_generation else None

    def retrieve_context(self, question: str) -> tuple[list[Hit], list[Hit], dict]:
        """Everything before the LLM. Returns (context hits, raw retrieved hits, trace)."""
        t0 = time.perf_counter()
        retrieved = self.retriever.retrieve(question)
        t1 = time.perf_counter()
        hits, trace = apply_filters(retrieved, self.cfg)
        if self.reranker:
            hits = self.reranker.rerank(question, hits)
        else:
            hits = hits[:self.cfg["reranker"]["top_n"]]  # same context size with and without reranker
        trace.update(retrieval_s=t1 - t0, rerank_s=time.perf_counter() - t1, context=len(hits))
        return hits, retrieved, trace

    def answer(self, question: str) -> RAGResult:
        start = time.perf_counter()
        hits, retrieved, trace = self.retrieve_context(question)

        if not hits:
            # nothing passed the similarity threshold: refuse without asking the LLM at all
            answer = REFUSALS[detect_question_lang(question)]
            trace.update(to_llm=0, generation_s=0.0, total_s=time.perf_counter() - start)
            return RAGResult(question, answer, True, [], retrieved, trace)

        messages, used = build_messages(self.cfg["generation"]["prompt"], question, hits,
                                        self.cfg["generation"]["max_context_chars"])
        answer, gen_s = self.llm.chat(messages)
        trace.update(to_llm=len(used), generation_s=gen_s, total_s=time.perf_counter() - start)
        return RAGResult(question, answer, is_refusal(answer), used, retrieved, trace)
