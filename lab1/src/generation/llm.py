"""Ollama chat client (local open-source models: Qwen, Llama, Gemma, ...)."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class OllamaLLM:
    def __init__(self, model: str, host: str = "http://localhost:11434", temperature: float = 0.1,
                 num_ctx: int = 4096, timeout_s: float = 300):
        self.model = model
        self.url = host.rstrip("/") + "/api/chat"
        self.options = {"temperature": temperature, "num_ctx": num_ctx}
        self.timeout_s = timeout_s

    def chat(self, messages: list[dict]) -> tuple[str, float]:
        """Returns (answer, seconds)."""
        start = time.perf_counter()
        try:
            resp = requests.post(self.url, json={"model": self.model, "messages": messages, "stream": False,
                                                 "options": self.options}, timeout=self.timeout_s)
        except requests.ConnectionError as exc:
            raise RuntimeError("Ollama is not running: start it (`ollama serve`) and pull the model "
                               f"(`ollama pull {self.model}`)") from exc
        if resp.status_code == 404:
            raise RuntimeError(f"Model {self.model} is not pulled: run `ollama pull {self.model}`")
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip(), time.perf_counter() - start
