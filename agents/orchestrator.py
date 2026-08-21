"""Athena orchestrator: data → sequential workers → conflict resolve → optional Flash narrative."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from agents.config import graph_enabled
from agents.conflict import resolve
from agents.fundamental import run_fundamental
from agents.graph import run_graph
from agents.protocol import ExecutionStatus, OverallAssessment
from agents.prompts import ORCHESTRATOR_NARRATIVE
from agents.state import set_last
from agents.status import set_status
from agents.technical import run_technical
from core.activity_logger import activity
from core.agent_debug_logger import debug, event
from data.acquire import acquire
from security.sanitize import sanitize

_lock = threading.Lock()


def _narrative(assessment: OverallAssessment) -> str:
    try:
        from core.gemini_models import get_flash_model
        from google import genai
        from memory.config_manager import get_gemini_key

        key = get_gemini_key()
        if not key:
            return ""
        payload = assessment.model_dump(mode="json")
        for k in ("fundamental", "technical", "graph"):
            block = payload.get(k)
            if isinstance(block, dict):
                block.pop("input_data", None)
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=get_flash_model(),
            contents=(
                ORCHESTRATOR_NARRATIVE
                + "\n\nStructured assessment JSON:\n"
                + str(payload)[:8000]
            ),
        )
        text = ""
        try:
            text = (resp.text or "").strip()
        except Exception:
            text = ""
        return sanitize(text)
    except Exception as e:
        debug(f"flash narrative: {e}", "warning")
        return ""


def _template_narrative(a: OverallAssessment) -> str:
    bits = [
        f"Overall assessment: {a.overall}.",
        a.why,
        a.recommendation,
    ]
    if a.layers_missing:
        bits.append("Missing layers: " + ", ".join(a.layers_missing) + ".")
    return " ".join(b for b in bits if b)


def run_market_analysis(symbol: str, timeframe: str = "H1") -> OverallAssessment:
    """Sequential GPU workers. Never calls order_send / trading_desk."""
    if not _lock.acquire(blocking=False):
        activity("Athena is already running a market analysis. Please wait.")
        raise RuntimeError("analysis already running")
    set_status("athena", "busy")
    try:
        activity(f"Athena started market analysis for {symbol} {timeframe}.")
        event("analysis_start", agent="athena", task_id=f"{symbol}:{timeframe}")
        snap = acquire(symbol, timeframe)
        if snap.ta.get("ok"):
            activity("Market data and indicators are ready.")
        else:
            activity("Market data is incomplete. Analysis may be limited.")

        technical = run_technical(snap)
        fundamental = run_fundamental(snap)
        graph = None
        if graph_enabled():
            graph = run_graph(snap)
        else:
            from agents.protocol import failed_result
            from agents.config import load_agent_config as _lac
            model = str((_lac().get("graph_agent") or {}).get("model") or "qwen2.5-math:7b")
            graph = failed_result(
                "graph", "graph_analysis", model, ExecutionStatus.DISABLED,
                "Graph agent is turned off.",
            )

        activity("Athena is combining findings. Technical analysis leads; news can only veto.")
        assessment = resolve(
            symbol=snap.symbol,
            timeframe=snap.timeframe,
            data_timestamp=snap.fetched_at,
            fundamental=fundamental,
            technical=technical,
            graph=graph,
            bar_time=int((snap.ta or {}).get("bar_time") or 0),
        )
        if assessment.disagreement:
            activity(f"Agents disagree. Overall: {assessment.overall}.")
        else:
            activity(f"Athena finished. Overall: {assessment.overall}.")

        set_status("athena", "busy")
        narrative = _narrative(assessment) or _template_narrative(assessment)
        assessment.narrative = narrative
        if not assessment.timestamp:
            assessment.timestamp = datetime.now(timezone.utc).isoformat()
        set_last(assessment)
        event(
            "analysis_done",
            agent="athena",
            status="ok",
            symbol=snap.symbol,
        )
        set_status("athena", "ready")
        set_status("deepseek", "ready" if fundamental.execution_status == ExecutionStatus.OK else "error")
        set_status("qwen", "ready" if technical.execution_status == ExecutionStatus.OK else "error")
        return assessment
    except Exception as e:
        set_status("athena", "error")
        debug(f"run_market_analysis: {e}", "error")
        raise
    finally:
        _lock.release()
