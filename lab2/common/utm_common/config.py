"""Configuration from environment variables (12-factor): the same image runs in compose and Kubernetes,
only the environment changes. Secrets (passwords, JWT key) never have defaults that look real."""
from __future__ import annotations

import os


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value or ""


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


SERVICE_NAME = env("SERVICE_NAME", "service")
LOG_LEVEL = env("LOG_LEVEL", "INFO")
