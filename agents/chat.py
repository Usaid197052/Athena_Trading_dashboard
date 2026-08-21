"""Gemini Flash HUD chat. Analysis only — never places orders."""
from __future__ import annotations

import threading

from security.sanitize import sanitize

_lock = threading.Lock()
_history: list[tuple[str, str]] = []
_MAX_TURNS = 8
_MAX_REPLY = 1800

CHAT_SYSTEM = """You are Athena, the trading-desk coordinator on a local Windows HUD.
Speak in short, clear sentences. You are interactive: answer the owner's question.

Your main job in chat is to explain the latest analysis: overall bias, specialist views,
and HOW CONFIDENT you are (or are not) about taking a trade.

How to explain confidence:
- Use CONFIDENCE BRIEF. Quote real percentages, engine score, and whether specialists disagree.
- Technical analysis LEADS. If fundamentals are neutral, weak, or missing, Athena follows the chart.
- High-impact fresh news can veto a weak technical signal; otherwise TA wins on conflict.
- Auto-trade only acts after agent analysis (same rules). Analyze alone never places an order.
- If TRADE CONVICTION is LOW or high-impact news vetoed the chart, say you are NOT confident taking a trade.
- If conviction is moderate or higher, still say it is probabilistic, not a guarantee.
- Never invent ticket numbers, fills, prices, or confidence numbers that are not in the brief.
- Default instrument is EURUSD. Other pairs are fine when the owner names them (analyze GBPUSD H1).
- If there is no Analyze result yet, tell them to press Analyze or type `analyze`, then ask again.

You never place, modify, or close trades from chat and you never call order_send.
If auto-trade is ON, Analyze / the watch loop places demo orders from the agent result.
If they need a manual push, tell them to type `run desk` or `run desk GBPUSD` (not bare words).
If auto-trade is paused, tell them to press RESUME.

Desk commands:
analyze [symbol] [timeframe] · pause · resume · flatten · status · run desk · quote · sleep · help
"""


def clear_history() -> None:
    with _lock:
        _history.clear()


def _snapshot() -> str:
    parts: list[str] = []
    try:
        from agents.state import format_confidence_brief
        parts.append(format_confidence_brief()[:4500])
    except Exception:
        pass
    try:
        from actions.trading_desk import hud_text
        parts.append(hud_text()[:2200])
    except Exception:
        pass
    try:
        from agents.state import format_hud_analysis
        extra = format_hud_analysis()
        if extra:
            parts.append(extra[:1500])
    except Exception:
        pass
    try:
        from agents.status import snapshot as agent_snapshot
        parts.append("AGENT STATUS  " + str(agent_snapshot()))
    except Exception:
        pass
    return "\n".join(p for p in parts if p) or "(no desk snapshot)"


def _pack_history() -> str:
    with _lock:
        rows = list(_history[-_MAX_TURNS:])
    if not rows:
        return "(none yet)"
    lines = []
    for user, assistant in rows:
        lines.append(f"Owner: {user}")
        lines.append(f"Athena: {assistant}")
    return "\n".join(lines)


def _remember(user: str, assistant: str) -> None:
    with _lock:
        _history.append((user, assistant))
        if len(_history) > _MAX_TURNS:
            del _history[:-_MAX_TURNS]


def reply(user_text: str) -> str:
    """Return Athena's chat reply. Raises RuntimeError if Gemini is unavailable."""
    from google import genai
    from core.gemini_models import get_flash_model
    from memory.config_manager import get_gemini_key

    text = (user_text or "").strip()
    if not text:
        return ""
    key = get_gemini_key()
    if not key:
        raise RuntimeError("Save a Gemini API key in Settings before chatting.")

    prompt = (
        CHAT_SYSTEM
        + "\n\nDESK SNAPSHOT:\n"
        + _snapshot()
        + "\n\nRECENT CHAT:\n"
        + _pack_history()
        + "\n\nOWNER JUST SAID:\n"
        + text
        + "\n\nReply as Athena. If they asked about confidence or taking a trade, "
        "lead with conviction (low / moderate / higher), quote the percentages, "
        "and say whether Analyze and auto-trade agree. No JSON. No API keys."
    )
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=get_flash_model(),
        contents=prompt,
    )
    out = ""
    try:
        out = (resp.text or "").strip()
    except Exception:
        out = ""
    if not out:
        try:
            for part in resp.candidates[0].content.parts:
                if getattr(part, "text", None):
                    out += part.text
            out = out.strip()
        except Exception:
            out = ""
    if not out:
        raise RuntimeError("Athena had no reply. Try again in a moment.")
    out = sanitize(out)[:_MAX_REPLY]
    _remember(text, out)
    return out
