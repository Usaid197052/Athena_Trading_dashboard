"""Acquire + normalize market data for agents. Agents never call providers directly."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agents.config import load_agent_config
from data.adapters.mt5 import fetch_market
from data.adapters.news import fetch_headlines
from data.snapshot import NormalizedMarketSnapshot


def acquire(symbol: str, timeframe: str = "H1") -> NormalizedMarketSnapshot:
    cfg = load_agent_config()
    snap = fetch_market(symbol, timeframe)
    if (not snap.ok) and cfg.get("yfinance_fallback"):
        from data.adapters.yfinance_adapter import fetch_yfinance
        alt = fetch_yfinance(symbol, timeframe)
        if alt.ok:
            alt.headlines = snap.headlines
            alt.calendar = snap.calendar
            snap = alt

    def _news():
        return fetch_headlines(snap.symbol)

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_news)
        try:
            heads, text = fut.result(timeout=16)
        except Exception:
            heads, text = [], "NEWS unavailable"
    snap.headlines = heads
    snap.news_text = text
    return snap
