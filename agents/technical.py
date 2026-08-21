"""Agent 2 — Qwen2.5-Coder technical interpretation (no code execution)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agents.config import load_agent_config
from agents.protocol import AgentResult, ExecutionStatus, failed_result, status_from
from agents.prompts import TECHNICAL_SYSTEM
from agents.runtime_ollama import OllamaError, chat_json_with_retry
from agents.status import set_status
from core.activity_logger import activity
from core.agent_debug_logger import event
from data.snapshot import NormalizedMarketSnapshot


def _user_payload(snap: NormalizedMarketSnapshot) -> str:
    ta = snap.compact_ta()
    body = {
        "symbol": snap.symbol,
        "timeframe": snap.timeframe,
        "engine": ta,
        "instruction": (
            "Interpret these engine numbers. Copy engine_score and engine_bias. "
            "Set agrees_with_engine true/false. Do not output Python. Do not invent prices."
        ),
    }
    return json.dumps(body, default=str)


def run_technical(snap: NormalizedMarketSnapshot) -> AgentResult:
    cfg = load_agent_config()
    spec = cfg.get("technical") or {}
    model = str(spec.get("model") or "qwen2.5-coder:7b")
    timeout = float(spec.get("timeout_sec") or 90)
    set_status("qwen", "busy")
    activity("Qwen is reviewing technical indicators.")
    event("agent_start", agent="technical", model=model, task_id=snap.symbol)
    if not snap.ta.get("ok"):
        set_status("qwen", "error")
        activity("Technical calculations were not available.")
        return failed_result(
            "technical", "technical_analysis", model,
            ExecutionStatus.UNAVAILABLE,
            str(snap.ta.get("error") or snap.error or "indicator engine failed"),
        )
    try:
        raw, elapsed = chat_json_with_retry(
            model, TECHNICAL_SYSTEM, _user_payload(snap), timeout_sec=timeout
        )
        raw.setdefault("agent", "technical")
        raw.setdefault("task", "technical_analysis")
        raw.setdefault("model", model)
        raw.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        raw.setdefault("execution_status", "ok")
        raw.setdefault("engine_score", int(snap.ta.get("score") or 0))
        raw.setdefault("engine_bias", str(snap.ta.get("bias") or "WAIT"))
        raw["input_data"] = {
            "symbol": snap.symbol,
            "tf": snap.timeframe,
            "engine_score": snap.ta.get("score"),
            "engine_bias": snap.ta.get("bias"),
        }
        result = AgentResult.model_validate(raw)
        activity(
            f"Technical review finished. Bias: {result.bias.value}. "
            "Calculations matched the built-in engine."
            if result.agrees_with_engine else
            f"Technical review finished. Bias: {result.bias.value}. "
            "Qwen did not fully agree with the built-in score — treat that as a warning."
        )
        event(
            "agent_done",
            agent="technical",
            model=model,
            status=result.execution_status.value,
            elapsed_ms=round(elapsed),
        )
        set_status("qwen", "ready")
        return result
    except OllamaError as e:
        activity("Qwen could not finish the technical review.")
        event("agent_done", agent="technical", model=model, status=e.status)
        set_status("qwen", "error")
        return failed_result("technical", "technical_analysis", model, status_from(e.status), str(e))
    except Exception as e:
        activity("Qwen could not finish the technical review.")
        set_status("qwen", "error")
        return failed_result(
            "technical", "technical_analysis", model, ExecutionStatus.ERROR, str(e)
        )
