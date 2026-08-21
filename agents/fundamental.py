"""Agent 1 — DeepSeek-R1 fundamental / market intelligence."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agents.config import load_agent_config
from agents.protocol import AgentResult, ExecutionStatus, failed_result, status_from
from agents.prompts import FUNDAMENTAL_SYSTEM
from agents.runtime_ollama import OllamaError, chat_json_with_retry
from agents.status import set_status
from core.activity_logger import activity
from core.agent_debug_logger import event
from data.snapshot import NormalizedMarketSnapshot


def _user_payload(snap: NormalizedMarketSnapshot) -> str:
    cal = [
        {"time": c.time, "level": c.level, "currency": c.currency, "name": c.name}
        for c in snap.calendar[:10]
    ]
    body = {
        "symbol": snap.symbol,
        "timeframe": snap.timeframe,
        "last_price": snap.ta.get("close") or snap.bid,
        "calendar": cal,
        "headlines": [h.model_dump() for h in snap.headlines[:12]],
        "news_text": (snap.news_text or "")[:3500],
        "instruction": (
            "Classify news for this asset. Count material vs low_impact vs noise. "
            "Do not invent headlines. Do not predict a price."
        ),
    }
    return json.dumps(body, default=str)


def run_fundamental(snap: NormalizedMarketSnapshot) -> AgentResult:
    cfg = load_agent_config()
    spec = cfg.get("fundamental") or {}
    model = str(spec.get("model") or "deepseek-r1:7b")
    timeout = float(spec.get("timeout_sec") or 180)
    set_status("deepseek", "busy")
    n_head = len(snap.headlines)
    activity(f"DeepSeek is reviewing market news for {snap.symbol}.")
    event("agent_start", agent="fundamental", model=model, task_id=snap.symbol)
    try:
        raw, elapsed = chat_json_with_retry(
            model, FUNDAMENTAL_SYSTEM, _user_payload(snap), timeout_sec=timeout
        )
        raw.setdefault("agent", "fundamental")
        raw.setdefault("task", "fundamental_analysis")
        raw.setdefault("model", model)
        raw.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        raw.setdefault("execution_status", "ok")
        raw["input_data"] = {
            "symbol": snap.symbol,
            "headline_count": n_head,
            "calendar_count": len(snap.calendar),
        }
        result = AgentResult.model_validate(raw)
        try:
            ft = datetime.fromisoformat(str(snap.fetched_at).replace("Z", "+00:00"))
            result.recency_sec = max(0, int((datetime.now(timezone.utc) - ft).total_seconds()))
        except Exception:
            result.recency_sec = 0
        counts = result.news_counts or {}
        material = int(counts.get("material") or 0)
        if material:
            activity(
                f"DeepSeek found {material} important event(s) that may influence {snap.symbol}."
            )
        else:
            activity("DeepSeek has finished reviewing today's market news.")
        event(
            "agent_done",
            agent="fundamental",
            model=model,
            status=result.execution_status.value,
            elapsed_ms=round(elapsed),
        )
        set_status("deepseek", "ready")
        return result
    except OllamaError as e:
        activity("DeepSeek could not finish the news review.")
        event("agent_done", agent="fundamental", model=model, status=e.status)
        set_status("deepseek", "error")
        return failed_result(
            "fundamental", "fundamental_analysis", model,
            status_from(e.status),
            str(e),
        )
    except Exception as e:
        activity("DeepSeek could not finish the news review.")
        set_status("deepseek", "error")
        return failed_result(
            "fundamental", "fundamental_analysis", model,
            ExecutionStatus.ERROR, str(e),
        )
