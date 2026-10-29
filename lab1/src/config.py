"""Configuration loading: YAML file + dotted-key overrides from experiments/CLI."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

LAB_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = LAB_ROOT / "configs" / "default.yaml"


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def set_dotted(cfg: dict, dotted_key: str, value: Any) -> None:
    """set_dotted(cfg, "retrieval.top_k", 10)"""
    node = cfg
    *parents, last = dotted_key.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[last] = value


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if overrides:
        for key, value in overrides.items():
            if "." in key:
                set_dotted(cfg, key, value)
            elif isinstance(value, dict):
                cfg = deep_merge(cfg, {key: value})
            else:
                cfg[key] = value
    return cfg


def resolve(path: str | Path) -> Path:
    """Paths in the config are relative to the lab1/ folder."""
    p = Path(path)
    return p if p.is_absolute() else LAB_ROOT / p
