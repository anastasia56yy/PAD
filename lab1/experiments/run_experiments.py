"""Runs every experiment group from configs/experiments.yaml and writes comparison tables.

    python -m src.cli experiments                 # all groups
    python -m src.cli experiments --only reranker top_k

Output: experiments/results/<group>/<variant>/{summary.json, per_question.jsonl}
        experiments/results/<group>.csv and experiments/results/REPORT.md
"""
from __future__ import annotations

import json
import logging

import pandas as pd
import yaml

from src.config import LAB_ROOT, load_config, resolve, set_dotted
from src.evaluation.run_eval import run_evaluation
from src.indexing import build_index
from src.preprocessing.process import process_documents

log = logging.getLogger(__name__)

# Columns shown in the report, depending on what the group measures
RETRIEVAL_COLS = ["hit@1", "hit@5", "hit@10", "recall@5", "mrr", "ctx_hit", "ctx_mrr", "ctx_precision",
                  "unanswerable_ctx_empty", "avg_retrieval_s", "avg_rerank_s"]
GENERATION_COLS = ["ctx_hit", "keyword_recall", "citation", "refusal_accuracy", "false_refusal_rate",
                   "correct_refusal_rate", "correctness", "faithfulness", "avg_latency_s"]


def variant_config(base_cfg: dict, group: dict, overrides: dict) -> dict:
    cfg = json.loads(json.dumps(base_cfg))  # deep copy
    for key, value in {**group.get("base", {}), **overrides}.items():
        set_dotted(cfg, key, value)
    return cfg


def run_group(name: str, group: dict, base_cfg: dict) -> pd.DataFrame:
    rows = []
    for variant, overrides in group["variants"].items():
        cfg = variant_config(base_cfg, group, overrides)
        cfg["paths"]["results_dir"] = str(resolve(base_cfg["paths"]["results_dir"]) / name)
        index_info = build_index(cfg, reprocess=False)
        try:
            summary = run_evaluation(cfg, with_generation=group["generation"], run_name=variant)
        except RuntimeError as exc:  # e.g. an Ollama model is not pulled: skip the variant, keep the group
            log.error("%s/%s skipped: %s", name, variant, exc)
            continue
        rows.append({"variant": variant, "chunks": index_info["chunks"], **summary})
    df = pd.DataFrame(rows).drop(columns=["by_type"], errors="ignore")
    df.to_csv(resolve(base_cfg["paths"]["results_dir"]) / f"{name}.csv", index=False)
    return df


def to_markdown(df: pd.DataFrame, cols: list[str]) -> str:
    present = ["variant", "chunks"] + [c for c in cols if c in df.columns]
    return df[present].to_markdown(index=False, floatfmt=".3f")


def run_all(base_cfg: dict | None = None, only: list[str] | None = None) -> None:
    base_cfg = base_cfg or load_config()
    with open(LAB_ROOT / "configs" / "experiments.yaml", encoding="utf-8") as f:
        groups = yaml.safe_load(f)
    resolve(base_cfg["paths"]["results_dir"]).mkdir(parents=True, exist_ok=True)
    process_documents(base_cfg)

    report = ["# Результаты экспериментов\n",
              "Сгенерировано `python -m src.cli experiments`. Метрики описаны в README (раздел Evaluation).\n"]
    for name, group in groups.items():
        if only and name not in only:
            continue
        log.info("=== Experiment group: %s ===", name)
        df = run_group(name, group, base_cfg)
        if df.empty:
            continue
        cols = GENERATION_COLS if group["generation"] else RETRIEVAL_COLS
        report += [f"\n## {name}\n", f"{group['description']}\n", to_markdown(df, cols), ""]

    out = resolve(base_cfg["paths"]["results_dir"]) / ("REPORT.md" if not only else f"REPORT_{'_'.join(only)}.md")
    out.write_text("\n".join(report), encoding="utf-8")
    log.info("Report written to %s", out)
