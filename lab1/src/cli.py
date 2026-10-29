"""Command line entry point.

    python -m src.cli grab                      # collect / update documents
    python -m src.cli index                     # clean, chunk, embed, store in Chroma
    python -m src.cli ask "Cum se calculează nota finală?"
    python -m src.cli eval                      # retrieval + answer metrics on the question set
    python -m src.cli experiments               # run every experiment from configs/experiments.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# model downloads/loading print progress bars and auth warnings on every run
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from .config import load_config  # noqa: E402


def parse_overrides(pairs: list[str]) -> dict:
    """--set retrieval.top_k=10 --set reranker.enabled=false"""
    import yaml
    overrides = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        overrides[key] = yaml.safe_load(value)
    return overrides


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rag")
    parser.add_argument("--config", default=None)
    parser.add_argument("--set", action="append", default=[], help="dotted override, e.g. retrieval.top_k=10")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("grab")
    sub.add_parser("index")
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true")
    ev = sub.add_parser("eval")
    ev.add_argument("--no-generation", action="store_true", help="retrieval metrics only (no LLM)")
    ex = sub.add_parser("experiments")
    ex.add_argument("--only", nargs="*", help="names of experiment groups to run")
    ex.add_argument("--resume", action="store_true", help="reuse variants that already have summary.json")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("huggingface_hub", "httpx", "chromadb", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config(args.config, parse_overrides(args.set))

    if args.command == "grab":
        from .grabber.run import Grabber
        print(json.dumps(Grabber(cfg).run(), indent=2))
    elif args.command == "index":
        from .indexing import build_index
        print(json.dumps(build_index(cfg), indent=2))
    elif args.command == "ask":
        from .pipeline import RAGPipeline
        result = RAGPipeline(cfg).answer(args.question)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(result.format())
    elif args.command == "eval":
        from .evaluation.run_eval import run_evaluation
        summary = run_evaluation(cfg, with_generation=not args.no_generation, run_name="cli")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "experiments":
        from experiments.run_experiments import run_all
        run_all(cfg, only=args.only, resume=args.resume)


if __name__ == "__main__":
    main()
