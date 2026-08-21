"""News adapter — DuckDuckGo with a longer timeout than the desk blackout path."""
from __future__ import annotations

import threading
from data.snapshot import Headline

_TIMEOUT = 15.0
_MAX = 10


def fetch_headlines(symbol: str, query: str | None = None) -> tuple[list[Headline], str]:
    q = (query or f"{symbol} forex market news today").strip()
    box: list[tuple[list[Headline], str]] = [([], "")]

    def _run() -> None:
        try:
            from actions.web_search import _ddg_news, _format_news
            rows = _ddg_news(q, max_results=_MAX) or []
            heads = [
                Headline(
                    title=str(r.get("title") or ""),
                    snippet=str(r.get("snippet") or ""),
                    url=str(r.get("url") or ""),
                    source=str(r.get("source") or ""),
                )
                for r in rows
                if r.get("title")
            ]
            text = _format_news(q, rows) if rows else ""
            box[0] = (heads, text or "")
        except Exception as e:
            box[0] = ([], f"NEWS unavailable: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(_TIMEOUT)
    if t.is_alive():
        return [], "NEWS skipped (timeout)"
    return box[0]
