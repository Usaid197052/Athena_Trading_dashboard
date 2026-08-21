"""Optional yfinance fallback — off unless config yfinance_fallback is true."""
from __future__ import annotations

from data.snapshot import Bar, NormalizedMarketSnapshot


def fetch_yfinance(symbol: str, timeframe: str = "H1") -> NormalizedMarketSnapshot:
    tf = (timeframe or "H1").upper()
    interval = {
        "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
        "H1": "60m", "H4": "60m", "D1": "1d", "W1": "1wk",
    }.get(tf, "60m")
    period = "5d" if interval.endswith("m") else "6mo"
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return NormalizedMarketSnapshot(
            symbol=symbol, timeframe=tf, source="yfinance", ok=False,
            error="yfinance is not installed",
        )
    try:
        hist = yf.Ticker(symbol).history(period=period, interval=interval)
        if hist is None or hist.empty:
            return NormalizedMarketSnapshot(
                symbol=symbol, timeframe=tf, source="yfinance", ok=False,
                error=f"yfinance returned no bars for {symbol}",
            )
        bars: list[Bar] = []
        for idx, row in hist.tail(120).iterrows():
            ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else 0
            bars.append(Bar(
                time=ts,
                open=float(row.get("Open") or 0),
                high=float(row.get("High") or 0),
                low=float(row.get("Low") or 0),
                close=float(row.get("Close") or 0),
                volume=float(row.get("Volume") or 0),
            ))
        last = bars[-1] if bars else None
        return NormalizedMarketSnapshot(
            symbol=symbol,
            timeframe=tf,
            source="yfinance",
            ok=bool(bars),
            bid=last.close if last else None,
            ask=last.close if last else None,
            bars=bars,
            ta={"ok": False, "error": "TA engine requires MT5 rates; yfinance is price-only fallback"},
        )
    except Exception as e:
        return NormalizedMarketSnapshot(
            symbol=symbol, timeframe=tf, source="yfinance", ok=False, error=str(e)
        )
