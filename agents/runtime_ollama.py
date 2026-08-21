"""Ollama HTTP runtime: one model at a time, unload after each call."""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from agents.config import load_agent_config
from core.agent_debug_logger import debug, event
from security.sanitize import sanitize

_THINK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


class OllamaError(Exception):
    def __init__(self, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status


def _host() -> str:
    return str(load_agent_config().get("ollama_host") or "http://127.0.0.1:11434").rstrip("/")


def available(timeout: float = 2.0) -> bool:
    try:
        r = requests.get(_host() + "/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def list_models(timeout: float = 3.0) -> list[str]:
    try:
        r = requests.get(_host() + "/api/tags", timeout=timeout)
        r.raise_for_status()
        names = []
        for m in (r.json().get("models") or []):
            n = str(m.get("name") or "")
            if n:
                names.append(n)
        return names
    except Exception:
        return []


def unload(model: str) -> None:
    if not model:
        return
    try:
        requests.post(
            _host() + "/api/generate",
            json={"model": model, "keep_alive": 0, "prompt": ""},
            timeout=30,
        )
        event("model_unload", model=model, status="ok")
        debug(f"unloaded {model}")
    except Exception as e:
        event("model_unload", model=model, status="error")
        debug(f"unload {model}: {e}", "warning")


def strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def extract_json(text: str) -> dict[str, Any]:
    raw = strip_think(text)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(raw[start:end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("model output was not valid JSON")


def chat_json(
    model: str,
    system: str,
    user: str,
    *,
    timeout_sec: float = 90,
    num_predict: int | None = None,
) -> tuple[dict[str, Any], float]:
    """Send a chat request. Always unloads the model afterwards."""
    cfg = load_agent_config()
    ctx = int(cfg.get("context_tokens") or 4096)
    pred = int(num_predict if num_predict is not None else cfg.get("num_predict") or 1024)
    keep = cfg.get("keep_alive") or "0s"
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": keep,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {
            "num_ctx": ctx,
            "num_predict": pred,
            "temperature": 0.2,
        },
    }
    t0 = time.perf_counter()
    event("model_load", model=model, status="start")
    try:
        r = requests.post(_host() + "/api/chat", json=payload, timeout=timeout_sec)
        elapsed = (time.perf_counter() - t0) * 1000
        if r.status_code == 404:
            raise OllamaError(f"Ollama does not have {model}. Pull it first.", "unavailable")
        if r.status_code >= 500:
            body = sanitize(r.text[:200])
            raise OllamaError(f"Ollama error {r.status_code}: {body}", "error")
        r.raise_for_status()
        data = r.json()
        content = ""
        msg = data.get("message") or {}
        content = str(msg.get("content") or data.get("response") or "")
        event(
            "model_generate",
            model=model,
            status="ok",
            elapsed_ms=round(elapsed),
            eval_count=data.get("eval_count"),
        )
        return extract_json(content), elapsed
    except requests.Timeout:
        raise OllamaError(f"{model} timed out after {timeout_sec:.0f}s", "timeout") from None
    except OllamaError:
        raise
    except requests.ConnectionError:
        raise OllamaError(
            "Ollama is not running. Start Ollama, then pull the local models.",
            "unavailable",
        ) from None
    except ValueError as e:
        raise OllamaError(str(e), "rejected") from e
    except Exception as e:
        raise OllamaError(sanitize(str(e)), "error") from e
    finally:
        unload(model)


def chat_json_with_retry(
    model: str,
    system: str,
    user: str,
    *,
    timeout_sec: float = 90,
) -> tuple[dict[str, Any], float]:
    try:
        return chat_json(model, system, user, timeout_sec=timeout_sec)
    except OllamaError as e:
        if e.status != "rejected":
            raise
        repair = (
            user
            + "\n\nYour previous reply was not valid JSON. "
            "Reply again with ONLY a JSON object matching the schema. No markdown."
        )
        return chat_json(model, system, repair, timeout_sec=timeout_sec)
