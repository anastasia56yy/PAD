"""Run the question set through the pipeline and compute metrics.

Writes experiments/results/<run_name>/per_question.jsonl and summary.json.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from statistics import mean

from tqdm import tqdm

from ..config import resolve
from ..generation.prompts import format_context
from ..pipeline import RAGPipeline, detect_text_lang, is_refusal
from .metrics import (has_citation, hit_at_k, keyword_recall, precision_at_k, ranked_docs, recall_at_k,
                      reciprocal_rank)

log = logging.getLogger(__name__)


def load_questions(cfg: dict) -> list[dict]:
    with open(resolve(cfg["paths"]["eval_file"]), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _avg(rows: list[dict], key: str) -> float | None:
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(mean(values), 4) if values else None


def evaluate_question(pipe: RAGPipeline, q: dict, k_values: list[int], with_generation: bool, judge=None) -> dict:
    relevant = set(q.get("relevant_docs", []))
    answerable = q["type"] != "unanswerable"
    row = {"id": q["id"], "type": q["type"], "lang": q.get("lang"), "question": q["question"]}

    if with_generation:
        result = pipe.answer(q["question"])
        context_hits, retrieved, trace = result.sources, result.retrieved, result.trace
    else:
        context_hits, retrieved, trace = pipe.retrieve_context(q["question"])
    row["trace"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in trace.items()}

    if answerable:
        raw_rank = ranked_docs(retrieved)
        ctx_rank = ranked_docs(context_hits)
        for k in k_values:
            row[f"hit@{k}"] = hit_at_k(raw_rank, relevant, k)
            row[f"recall@{k}"] = recall_at_k(raw_rank, relevant, k)
            row[f"precision@{k}"] = precision_at_k(raw_rank, relevant, k)
        row["mrr"] = reciprocal_rank(raw_rank, relevant)
        # what the LLM actually receives, after filters + reranker
        row["ctx_hit"] = hit_at_k(ctx_rank, relevant, len(ctx_rank) or 1)
        row["ctx_mrr"] = reciprocal_rank(ctx_rank, relevant)
        row["ctx_precision"] = precision_at_k(ctx_rank, relevant, len(ctx_rank) or 1)
    else:
        row["ctx_empty"] = float(len(context_hits) == 0)

    if with_generation:
        row["answer"] = result.answer
        row["refused"] = float(result.refused or is_refusal(result.answer))
        row["refusal_correct"] = float(row["refused"] == (0.0 if answerable else 1.0))
        row["latency_s"] = round(trace.get("total_s", 0.0), 2)
        row["lang_match"] = float(detect_text_lang(result.answer) == q.get("lang"))
        row["invalid_citations"] = float(trace.get("invalid_citations", 0) > 0)
        if answerable:
            row["keyword_recall"] = keyword_recall(result.answer, q.get("keywords", []))
            row["citation"] = float(has_citation(result.answer))
        if judge is not None and answerable and context_hits:
            row.update(judge.grade(q["question"], q.get("expected_answer", ""), format_context(context_hits),
                                   result.answer))
    return row


def summarize(rows: list[dict], k_values: list[int]) -> dict:
    answerable = [r for r in rows if r["type"] != "unanswerable"]
    unanswerable = [r for r in rows if r["type"] == "unanswerable"]
    keys = ([f"hit@{k}" for k in k_values] + [f"recall@{k}" for k in k_values] +
            [f"precision@{k}" for k in k_values] + ["mrr", "ctx_hit", "ctx_mrr", "ctx_precision",
                                                     "keyword_recall", "citation", "correctness", "faithfulness"])
    summary = {"n_questions": len(rows), "n_answerable": len(answerable), "n_unanswerable": len(unanswerable)}
    summary.update({k: _avg(answerable, k) for k in keys if _avg(answerable, k) is not None})
    if unanswerable:
        summary["unanswerable_ctx_empty"] = _avg(unanswerable, "ctx_empty")
    if any("refused" in r for r in rows):
        summary["refusal_accuracy"] = _avg(rows, "refusal_correct")
        summary["false_refusal_rate"] = _avg(answerable, "refused")
        summary["correct_refusal_rate"] = _avg(unanswerable, "refused")
        summary["avg_latency_s"] = _avg(rows, "latency_s")
        summary["lang_match"] = _avg(rows, "lang_match")
        summary["invalid_citation_rate"] = _avg(rows, "invalid_citations")
    summary["avg_retrieval_s"] = round(mean(r["trace"].get("retrieval_s", 0) for r in rows), 3)
    summary["avg_rerank_s"] = round(mean(r["trace"].get("rerank_s", 0) for r in rows), 3)

    by_type = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    summary["by_type"] = {
        t: {k: _avg(rs, k) for k in ("hit@5", "mrr", "ctx_hit", "keyword_recall", "refusal_correct", "correctness")
            if _avg(rs, k) is not None}
        for t, rs in by_type.items()
    }
    return summary


def run_evaluation(cfg: dict, with_generation: bool = True, run_name: str = "default",
                   pipeline: RAGPipeline | None = None) -> dict:
    ev = cfg.get("evaluation", {})
    k_values = ev.get("k_values", [1, 3, 5, 10, 20])
    questions = load_questions(cfg)
    pipe = pipeline or RAGPipeline(cfg, with_generation=with_generation)

    judge = None
    if with_generation and ev.get("judge", {}).get("enabled"):
        from .judge import Judge
        judge = Judge(ev["judge"]["model"], cfg["generation"]["host"])

    rows = [evaluate_question(pipe, q, k_values, with_generation, judge)
            for q in tqdm(questions, desc=run_name, leave=False)]
    summary = summarize(rows, k_values)

    out_dir = resolve(cfg["paths"]["results_dir"]) / run_name
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "per_question.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("%s: %s", run_name, {k: v for k, v in summary.items() if k != "by_type"})
    return summary
