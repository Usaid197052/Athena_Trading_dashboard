"""Agent 3 — optional numerical / graph worker. Disabled by default. No model pull."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agents.config import graph_enabled, load_agent_config
from agents.protocol import AgentResult, ExecutionStatus, failed_result, status_from
from agents.prompts import GRAPH_SYSTEM
from agents.runtime_ollama import OllamaError, chat_json_with_retry
from agents.status import set_status
from core.activity_logger import activity
from data.snapshot import NormalizedMarketSnapshot


def run_graph(snap: NormalizedMarketSnapshot) -> AgentResult:
    cfg = load_agent_config()
    spec = cfg.get("graph_agent") or {}
    model = str(spec.get("model") or "qwen2.5-math:7b")
    if not graph_enabled():
        set_status("graph", "disabled")
        return failed_result(
            "graph", "graph_analysis", model, ExecutionStatus.DISABLED,
            "Graph agent is turned off.",
        )
    timeout = float(spec.get("timeout_sec") or 90)
    set_status("graph", "busy")
    activity("The graph agent is reviewing numerical market structure.")
    bars = [b.model_dump() for b in snap.bars[-40:]]
    user = json.dumps({
        "symbol": snap.symbol,
        "timeframe": snap.timeframe,
        "spread": snap.spread,
        "ta": snap.compact_ta(),
        "recent_bars": bars,
        "instruction": "Use the numbers only. No screenshots. No price targets.",
    }, default=str)
    try:
        raw, _elapsed = chat_json_with_retry(
            model, GRAPH_SYSTEM, user, timeout_sec=timeout
        )
        raw.setdefault("agent", "graph")
        raw.setdefault("task", "graph_analysis")
        raw.setdefault("model", model)
        raw.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        raw.setdefault("execution_status", "ok")
        result = AgentResult.model_validate(raw)
        activity("Graph analysis finished.")
        set_status("graph", "ready")
        return result
    except OllamaError as e:
        activity("Graph analysis was skipped because the model was not available.")
        set_status("graph", "error")
        return failed_result("graph", "graph_analysis", model, status_from(e.status), str(e))
    except Exception as e:
        set_status("graph", "error")
        return failed_result("graph", "graph_analysis", model, ExecutionStatus.ERROR, str(e))
