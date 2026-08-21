"""Normalized market snapshot shared by all agents. No MT5 types."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Bar(BaseModel):
    time: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    spread: float | None = None


class CalendarEvent(BaseModel):
    time: str = ""
    level: str = ""
    currency: str = ""
    name: str = ""


class Headline(BaseModel):
    title: str = ""
    snippet: str = ""
    url: str = ""
    source: str = ""


class NormalizedMarketSnapshot(BaseModel):
    symbol: str
    timeframe: str = "H1"
    source: str = "mt5"
    ok: bool = True
    error: str = ""
    fetched_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    digits: int = 5
    bars: list[Bar] = Field(default_factory=list)
    tick: dict[str, Any] = Field(default_factory=dict)
    calendar: list[CalendarEvent] = Field(default_factory=list)
    headlines: list[Headline] = Field(default_factory=list)
    news_text: str = ""
    ta: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    def compact_ta(self) -> dict[str, Any]:
        keys = (
            "ok", "symbol", "tf", "close", "bid", "ask", "spread",
            "ema20", "ema50", "ema200", "trend", "rsi", "macd_hist", "atr",
            "bb_u", "bb_l", "bb_pos", "support", "resistance", "candle",
            "signal", "bias", "score", "conf", "reasons", "digits", "bar_time",
        )
        return {k: self.ta.get(k) for k in keys if k in self.ta}

    def news_blob(self, limit: int = 12) -> str:
        if self.news_text:
            return self.news_text[:4000]
        lines = []
        for h in self.headlines[:limit]:
            bit = h.title
            if h.source:
                bit += f" [{h.source}]"
            if h.snippet:
                bit += f" — {h.snippet[:180]}"
            lines.append(bit)
        return "\n".join(lines)
