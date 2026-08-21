# web_search.py
import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    try:
        from memory.config_manager import get_gemini_key
        return get_gemini_key() or ""
    except Exception:
        pass
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("gemini_api_key") or ""


from core.gemini_models import get_flash_model


def _wlog(msg: str, level: str = "info") -> None:
    print(f"[WebSearch] {msg}")
    try:
        from core.logger import log as athena_log
        athena_log(f"WebSearch: {msg}", level)
    except Exception:
        pass


def _quota_error(exc: BaseException) -> bool:
    s = str(exc)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower()


def _ddgs_cls():
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS
        return DDGS


def _hit(r: dict) -> dict:
    return {
        "title": r.get("title") or "",
        "snippet": r.get("body") or r.get("snippet") or "",
        "url": r.get("href") or r.get("url") or "",
        "source": r.get("source") or "",
    }


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    DDGS = _ddgs_cls()
    last_err = None
    attempts = ({}, {"backend": "auto"}, {"backend": "html"})
    for extra in attempts:
        try:
            client = DDGS()
            try:
                raw = client.text(query, max_results=max_results, **extra)
                rows = [_hit(r) for r in (raw or [])]
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            rows = [r for r in rows if r["title"] or r["snippet"]]
            if rows:
                return rows
        except TypeError:
            continue
        except Exception as e:
            last_err = e
            _wlog(f"DDG text failed ({extra or 'default'}): {e}", "warning")
    if last_err:
        _wlog(f"DDG search empty after retries: {last_err}", "warning")
    return []


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    DDGS = _ddgs_cls()
    try:
        client = DDGS()
        try:
            raw = client.news(query, max_results=max_results)
            rows = [_hit(r) for r in (raw or [])]
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        rows = [r for r in rows if r["title"]]
        if rows:
            return rows
    except Exception as e:
        _wlog(f"DDG news() failed ({e}) — falling back to text search", "warning")
    return _ddg_search(query, max_results=max_results)


def _wiki_lookup(query: str) -> str:
    """Last-resort Wikipedia search + summary. No API key."""
    try:
        import requests
    except ImportError:
        return ""
    headers = {"User-Agent": "AthenaAssistant/1.0 (desktop assistant)"}
    try:
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "utf8": 1,
                "format": "json",
                "srlimit": 1,
            },
            timeout=8,
            headers=headers,
        )
        r.raise_for_status()
        hits = (r.json().get("query") or {}).get("search") or []
        if not hits:
            return ""
        title = (hits[0].get("title") or "").strip()
        if not title:
            return ""
        s = requests.get(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title, safe=""),
            timeout=8,
            headers=headers,
        )
        s.raise_for_status()
        data = s.json()
        extract = (data.get("extract") or "").strip()
        if len(extract) < 40:
            return ""
        url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page") or ""
        lines = [f"Wikipedia: {title}", extract]
        if url:
            lines.append(url)
        return "\n".join(lines)
    except Exception as e:
        _wlog(f"Wikipedia lookup failed: {e}", "warning")
        return ""


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return ""
    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):
            lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return ""
    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _gemini_search(query: str) -> str:
    from google import genai

    client = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model=get_flash_model(),
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _web_fallback(query: str, max_results: int = 6) -> str:
    rows = _ddg_search(query, max_results=max_results)
    formatted = _format_ddg(query, rows)
    if formatted:
        return formatted
    wiki = _wiki_lookup(query)
    if wiki:
        return wiki
    return ""


def _useful(text: str | None) -> bool:
    t = (text or "").strip()
    if len(t) < 50:
        return False
    low = t.lower()
    if low.startswith("no results") or low.startswith("no news") or low.startswith("search failed"):
        return False
    return True


def _with_web_fallback(query: str, gemini_query: str | None = None, *, max_results: int = 6) -> str:
    """Prefer Gemini grounded search, but do not stall on quota — DDG/Wikipedia run in parallel."""
    gq = gemini_query or query
    gemini_hit: list[str] = []
    web_hit: list[str] = []
    g_err: list[BaseException] = []
    lock = threading.Lock()

    def try_g():
        try:
            text = _gemini_search(gq)
            with lock:
                gemini_hit.append(text)
        except Exception as e:
            g_err.append(e)
            _wlog(f"Gemini failed: {e}", "warning")

    def try_w():
        try:
            text = _web_fallback(query, max_results=max_results)
            if text:
                with lock:
                    web_hit.append(text)
        except Exception as e:
            _wlog(f"Web fallback failed: {e}", "warning")

    threading.Thread(target=try_g, daemon=True).start()
    threading.Thread(target=try_w, daemon=True).start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10.0:
        with lock:
            if gemini_hit and _useful(gemini_hit[0]):
                return gemini_hit[0]
            if web_hit and _useful(web_hit[0]) and (time.monotonic() - t0) >= 1.2:
                _wlog("Using DuckDuckGo/Wikipedia fallback (Gemini slow or rate-limited)")
                return web_hit[0]
        time.sleep(0.08)
    with lock:
        if gemini_hit and _useful(gemini_hit[0]):
            return gemini_hit[0]
        if web_hit and _useful(web_hit[0]):
            return web_hit[0]
    if g_err and _quota_error(g_err[0]):
        return (
            "Search failed: Gemini is rate-limited right now and the web fallback "
            f"found nothing for: {query}. Try again in a minute."
        )
    return f"No results found for: {query}"


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Falls back to DuckDuckGo news when Flash is rate-limited.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from google import genai

    try:
        client = genai.Client(api_key=_get_api_key())
        response = client.models.generate_content(
            model=get_flash_model(),
            contents=f"Current world news: {n} headlines. Numbered list, titles only.",
            config={"tools": [{"google_search": {}}]},
        )
        raw = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                raw += part.text
        headlines = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if not re.match(r"^[\d]+[.\)\-]", line):
                continue
            clean = re.sub(r"^[\d]+[.\)\-]\s*", "", line)
            clean = re.sub(r"^\*+\s*", "", clean).strip()
            if clean and len(clean) > 10:
                headlines.append(clean)
        if headlines:
            return headlines[:n], raw.strip()
    except Exception as e:
        _wlog(f"Gemini headlines failed: {e}", "warning")

    rows = _ddg_news("world news today", max_results=n)
    titles = [r["title"] for r in rows if r.get("title")][:n]
    return titles, _format_news("world news today", rows) or ""


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    return _with_web_fallback(query)


def _news(query: str) -> str:
    """Gemini grounded search and DDG news in parallel; first useful result wins."""
    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query = query if query else "world news today"

    result_box: list[str | None] = [None]
    lock = threading.Lock()
    done_evt = threading.Event()
    failures = [0]

    def _store(r: str) -> None:
        if _useful(r):
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:
                    done_evt.set()

    def _try_gemini():
        try:
            _store(_gemini_search(gemini_query))
        except Exception as e:
            _wlog(f"Gemini news failed: {e}", "warning")
            _store("")

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            _store(_format_news(ddg_query, results))
        except Exception as e:
            _wlog(f"DDG news failed: {e}", "warning")
            _store("")

    threading.Thread(target=_try_gemini, daemon=True).start()
    threading.Thread(target=_try_ddg, daemon=True).start()
    done_evt.wait(timeout=10.0)
    return result_box[0] or f"No news found for: {query}"


def _research(query: str) -> str:
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    return _with_web_fallback(query, research_query, max_results=10)


def _price(query: str) -> str:
    price_query = f"current price of {query} — how much does it cost today"
    return _with_web_fallback(f"{query} price buy", price_query)


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        _wlog(f"Gemini compare failed: {e} — falling back to DDG", "warning")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    any_hit = False
    for item in items:
        lines.append(f"\n▸ {item}")
        rows = all_results.get(item, [])[:2]
        if rows:
            any_hit = True
        for r in rows:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    if not any_hit:
        return _with_web_fallback(" vs ".join(items) + f" {aspect}", query)
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query = params.get("query", "").strip()
    mode = params.get("mode", "search").lower().strip()
    items = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    _wlog(f"mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        _wlog(f"All backends failed: {e}", "error")
        return f"Search failed: {e}"
