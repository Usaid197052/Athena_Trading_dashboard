"""MT5 market adapter — wraps actions.mt5_analysis. Agents must not import MetaTrader5."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from data.snapshot import Bar, CalendarEvent, NormalizedMarketSnapshot


def fetch_market(symbol: str, timeframe: str = "H1") -> NormalizedMarketSnapshot:
    from actions.mt5_analysis import (
        _copy_rates,
        _norm_symbol,
        _select,
        _timeframe,
        calendar_events,
        get_ta_metrics,
        pair_currencies,
        _ensure_mt5,
    )

    sym = _norm_symbol(symbol)
    err = _ensure_mt5()
    if err:
        return NormalizedMarketSnapshot(
            symbol=sym, timeframe=timeframe.upper(), ok=False, error=str(err), source="mt5"
        )
    miss = _select(sym)
    if miss:
        return NormalizedMarketSnapshot(
            symbol=sym, timeframe=timeframe.upper(), ok=False, error=str(miss), source="mt5"
        )

    ta = get_ta_metrics(sym, timeframe)
    bars: list[Bar] = []
    try:
        rates = _copy_rates(sym, _timeframe(timeframe))
        if rates is not None:
            for row in rates[-120:]:
                vol = 0.0
                try:
                    vol = float(row["tick_volume"])
                except Exception:
                    try:
                        vol = float(row["real_volume"])
                    except Exception:
                        vol = 0.0
                bars.append(Bar(
                    time=int(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=vol,
                ))
    except Exception:
        pass

    cal_rows: list[CalendarEvent] = []
    try:
        for e in calendar_events(pair_currencies(sym), hours=48)[:12]:
            t = e.get("time")
            cal_rows.append(CalendarEvent(
                time=t.isoformat() if isinstance(t, datetime) else str(t or ""),
                level=str(e.get("level") or ""),
                currency=str(e.get("currency") or ""),
                name=str(e.get("name") or ""),
            ))
    except Exception:
        pass

    tick: dict[str, Any] = {}
    if ta.get("ok"):
        tick = {
            "bid": ta.get("bid"),
            "ask": ta.get("ask"),
            "spread": ta.get("spread"),
            "close": ta.get("close"),
        }

    ok = bool(ta.get("ok"))
    return NormalizedMarketSnapshot(
        symbol=sym,
        timeframe=str(ta.get("tf") or timeframe).upper(),
        source="mt5",
        ok=ok,
        error="" if ok else str(ta.get("error") or "TA failed"),
        bid=ta.get("bid"),
        ask=ta.get("ask"),
        spread=ta.get("spread"),
        digits=int(ta.get("digits") or 5),
        bars=bars,
        tick=tick,
        calendar=cal_rows,
        ta=dict(ta) if isinstance(ta, dict) else {},
    )
