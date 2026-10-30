"""Recompute answer-level metrics from saved per_question.jsonl files after a metric fix,
without generating the answers again.

    python -m src.evaluation.rescore experiments/results/llm experiments/results/prompt ...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import load_config
from ..pipeline import is_refusal
from .run_eval import summarize


def rescore(run_dir: Path, k_values: list[int]) -> bool:
    per_q = run_dir / "per_question.jsonl"
    if not per_q.exists():
        return False
    rows = [json.loads(line) for line in per_q.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not any("answer" in r for r in rows):
        return False
    for r in rows:
        r["refused"] = float(is_refusal(r["answer"]))
        r["refusal_correct"] = float(r["refused"] == (1.0 if r["type"] == "unanswerable" else 0.0))
    per_q.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summarize(rows, k_values), ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    return True


if __name__ == "__main__":
    k_values = load_config().get("evaluation", {}).get("k_values", [1, 3, 5, 10, 20])
    for group in sys.argv[1:]:
        for run in sorted(Path(group).iterdir()):
            if run.is_dir():
                print(run, "rescored" if rescore(run, k_values) else "skipped (no saved answers)")
