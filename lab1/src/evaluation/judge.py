"""LLM-as-a-Judge: faithfulness and correctness of a generated answer.

The judge sees the question, the reference answer and the context that the RAG
model received, and returns JSON. A local model is a weak judge, so these
scores are reported next to the deterministic metrics (keyword recall,
refusal accuracy), never instead of them.
"""
from __future__ import annotations

import json
import re

from ..generation.llm import OllamaLLM

JUDGE_PROMPT = """You are grading the answer of a question-answering system about a university.

Question: {question}
Reference answer: {expected}
Context given to the system:
{context}

System answer: {answer}

Grade strictly and return ONLY a JSON object:
{{"correctness": 0|1|2, "faithfulness": 0|1}}
- correctness: 2 = matches the reference answer, 1 = partially correct or incomplete, 0 = wrong or missing.
- faithfulness: 1 = every claim in the system answer is supported by the context, 0 = it contains claims not in the context."""


class Judge:
    def __init__(self, model: str, host: str):
        self.llm = OllamaLLM(model, host, temperature=0.0, num_ctx=8192)

    def grade(self, question: str, expected: str, context: str, answer: str) -> dict:
        prompt = JUDGE_PROMPT.format(question=question, expected=expected, context=context[:6000], answer=answer)
        raw, _ = self.llm.chat([{"role": "user", "content": prompt}])
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        try:
            data = json.loads(match.group(0)) if match else {}
            return {"correctness": int(data["correctness"]), "faithfulness": int(data["faithfulness"])}
        except (ValueError, KeyError, TypeError):
            return {"correctness": None, "faithfulness": None, "judge_raw": raw[:300]}
