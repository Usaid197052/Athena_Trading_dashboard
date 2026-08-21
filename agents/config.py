"""Load config/agents.json with safe defaults."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_PATH = _base_dir() / "config" / "agents.json"

_DEFAULTS: dict[str, Any] = {
    "ollama_host": "http://127.0.0.1:11434",
    "models_dir": r"E:\Ollama models",
    "context_tokens": 4096,
    "num_predict": 1024,
    "keep_alive": "0s",
    "fundamental": {"model": "deepseek-r1:7b", "timeout_sec": 180},
    "technical": {"model": "qwen2.5-coder:7b", "timeout_sec": 90},
    "graph_agent": {"enabled": False, "model": "qwen2.5-math:7b", "timeout_sec": 90},
    "orchestrator": {"timeout_sec": 45},
    "yfinance_fallback": False,
}


def load_agent_config() -> dict[str, Any]:
    data = json.loads(json.dumps(_DEFAULTS))
    try:
        if _PATH.exists():
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict) and isinstance(data.get(k), dict):
                        data[k].update(v)
                    else:
                        data[k] = v
    except Exception:
        pass
    return data


def graph_enabled() -> bool:
    cfg = load_agent_config()
    ga = cfg.get("graph_agent") or {}
    return bool(ga.get("enabled"))
