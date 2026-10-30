"""The full RAG pipeline: retrieve -> filter -> rerank -> generate (with refusal when context is missing)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .generation.llm import OllamaLLM
from .generation.prompts import LANGUAGE_NAMES, REFUSALS, TRANSLATE, build_messages
from .reranking.reranker import Reranker
from .retrieval.filters import apply_filters
from .retrieval.retriever import Retriever
from .retrieval.vector_store import Hit

RO_HINTS = re.compile(r"[ăâîșțşţ]|\b(care|este|sunt|cum|unde|când|cine|ce|pentru|și|la|de)\b", re.IGNORECASE)


def detect_question_lang(question: str) -> str:
    if re.search(r"[а-яё]", question, re.IGNORECASE):
        return "ru"
    return "ro" if RO_HINTS.search(question) else "en"


RO_WORDS = {"și", "si", "este", "sunt", "pentru", "care", "de", "la", "în", "cu", "din", "o", "un", "nu", "se"}
EN_WORDS = {"the", "is", "are", "and", "of", "to", "in", "for", "with", "a", "an", "be", "must", "not"}


def detect_text_lang(text: str) -> str:
    """Language of a longer text (an answer), by word statistics: quoted Romanian titles inside an
    English answer must not flip the result, which a single hint word would."""
    letters = re.findall(r"[^\W\d_]", text)
    if letters and sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / len(letters) > 0.3:
        return "ru"
    words = re.findall(r"\w+", text.lower())
    ro = sum(w in RO_WORDS for w in words) + 2 * len(re.findall(r"[ăâîșț]", text.lower()))
    en = sum(w in EN_WORDS for w in words)
    return "ro" if ro > en else "en"


def is_refusal(answer: str) -> bool:
    low = answer.lower()
    return any(r.lower().rstrip(".") in low for r in REFUSALS.values())


def sanitize_citations(answer: str, n_sources: int) -> tuple[str, int]:
    """Drop citations that point outside [1..n_sources] (the model copying article numbers like "[85]").
    Returns the cleaned answer and how many citations were removed."""
    removed = 0

    def check(match: re.Match) -> str:
        nonlocal removed
        if 1 <= int(match.group(1)) <= n_sources:
            return match.group(0)
        removed += 1
        return ""

    cleaned = re.sub(r"\s?\[(\d+)\]", check, answer)
    return cleaned, removed


@dataclass
class RAGResult:
    question: str
    answer: str
    refused: bool
    sources: list[Hit]
    retrieved: list[Hit] = field(default_factory=list)   # raw top-K before filters
    trace: dict = field(default_factory=dict)
    confidence: str = "high"                             # high | low | none (see RAGPipeline.retrieve_context)
    related: list[Hit] = field(default_factory=list)     # best reranked chunks, shown as "similar documents" on refusal

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
        # thresholds are applied here, not inside the reranker, so the best candidates are known even when
        # all of them are below the threshold (they become "similar documents" on a refusal)
        self.reranker = Reranker(rr["model"], rr["top_n"], None, cfg["embeddings"]["device"]) if rr["enabled"] else None
        g = cfg["generation"]
        self.llm = OllamaLLM(g["model"], g["host"], g["temperature"], g["num_ctx"]) if with_generation else None

    def _apply_confidence(self, ranked: list[Hit]) -> tuple[list[Hit], str]:
        """Two-tier reranker threshold.

        score >= min_score       -> confident context
        soft_min_score <= score  -> low confidence: the LLM still gets the context and decides itself
                                    (keywords and vague questions like "магистратура" score below 0
                                    even when the right document is first)
        below soft_min_score     -> no context, refusal without calling the LLM
        """
        rr = self.cfg["reranker"]
        hard, soft = rr.get("min_score"), rr.get("soft_min_score")
        confident = [h for h in ranked if hard is None or h.score >= hard]
        if confident:
            return confident, "high"
        low = [h for h in ranked if soft is not None and h.score >= soft]
        return low, ("low" if low else "none")

    def retrieve_context(self, question: str) -> tuple[list[Hit], list[Hit], dict]:
        """Everything before the LLM. Returns (context hits, raw retrieved hits, trace)."""
        t0 = time.perf_counter()
        retrieved = self.retriever.retrieve(question)
        t1 = time.perf_counter()
        hits, trace = apply_filters(retrieved, self.cfg)
        if self.reranker:
            ranked = self.reranker.rerank(question, hits)
            hits, confidence = self._apply_confidence(ranked)
            trace["ranked"] = ranked
        else:
            hits = hits[:self.cfg["reranker"]["top_n"]]  # same context size with and without reranker
            confidence = "high" if hits else "none"
        trace.update(retrieval_s=t1 - t0, rerank_s=time.perf_counter() - t1, context=len(hits), confidence=confidence)
        return hits, retrieved, trace

    def answer(self, question: str) -> RAGResult:
        start = time.perf_counter()
        hits, retrieved, trace = self.retrieve_context(question)
        related = trace.pop("ranked", [])[:3]
        confidence = trace["confidence"]

        if not hits:
            # nothing passed the thresholds: refuse without asking the LLM at all
            answer = REFUSALS[detect_question_lang(question)]
            trace.update(to_llm=0, generation_s=0.0, total_s=time.perf_counter() - start)
            return RAGResult(question, answer, True, [], retrieved, trace, confidence, related)

        lang = detect_question_lang(question)
        prompt = self.cfg["generation"]["prompt"]
        messages, used = build_messages(prompt, question, hits, self.cfg["generation"]["max_context_chars"], lang)
        answer, gen_s = self.llm.chat(messages)
        translated = False
        if prompt == "strict_v2" and not is_refusal(answer) and detect_text_lang(answer) != lang:
            # 3B models drift into the language of the (mostly Romanian) context; one extra short call fixes it
            answer, extra_s = self.llm.chat([{"role": "user", "content": TRANSLATE.format(
                language=LANGUAGE_NAMES[lang], answer=answer)}])
            gen_s += extra_s
            translated = True
        answer, bad_citations = sanitize_citations(answer, len(used))
        trace.update(to_llm=len(used), generation_s=gen_s, total_s=time.perf_counter() - start,
                     invalid_citations=bad_citations, translated=translated)
        return RAGResult(question, answer, is_refusal(answer), used, retrieved, trace, confidence, related)
