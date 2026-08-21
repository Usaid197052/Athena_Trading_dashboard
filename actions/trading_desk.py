"""
Trading desk: agent analysis (TA leads) → gates → demo executor.
Old NumPy scorecard is an input to Qwen, not a separate order path.
"""
from __future__ import annotations

import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from actions.mt5_analysis import (
    calendar_events,
    get_ta_metrics,
    news_headlines,
    pair_currencies,
)
from actions.mt5_executor import (
    MAGIC_DEFAULT,
    account_snapshot,
    close_magic,
    daily_pnl,
    is_demo_account,
    magic_positions,
    place_market,
)

from core.analysis_logger import log_analysis
from core.trading_logger import decision as log_decision
from core.trading_logger import tlog

_lock = threading.Lock()
_runtime_paused = False
_last_bias = "WAIT"
_last_block = ""
_last_score = 0
_last_status = ""
_last_brief: dict[str, str] = {}
_last_fire: dict[str, tuple[int, str]] = {}
_last_eval: dict[str, tuple[int, str, str]] = {}


def last_autotrade_brief() -> dict[str, str]:
    """Last watch-loop / run-desk scorecard. Separate from agent Analyze."""
    return dict(_last_brief)

_NEWS_KEYS = (
    "nfp", "non-farm", "nonfarm", "non farm",
    "cpi", "fomc", "interest rate", "rate decision",
    "gdp", "ecb", "boe ", "bank of england",
)
_FA_TIME = re.compile(
    r"(\d{1,2}:\d{2}|\bin\s+\d+\s+min|\bwithin\s+\d+\s+min|"
    r"\bat\s+\d{1,2}(:\d{2})?\s*(am|pm|et|gmt|utc)?)",
    re.I,
)

_DEFAULTS = {
    "symbols": ["EURUSD"],
    "timeframe": "H1",
    "volume": 0.01,
    "sl_atr": 1.5,
    "tp_atr": 2.0,
    "magic": MAGIC_DEFAULT,
    "max_positions": 3,
    "daily_loss_pct": 3.0,
    "max_spread_points": 25,
    "deviation": 20,
    "auto_trade": True,
    "session_24h": True,
    "session_start_utc": "07:00",
    "session_end_utc": "21:00",
    "news_blackout_before_min": 30,
    "news_blackout_after_min": 15,
    "watch_interval_sec": 20,
    "comment": "Athena",
}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_CFG_PATH = _base_dir() / "config" / "trading.json"
_JOURNAL_PATH = _base_dir() / "memory" / "trading_journal.json"


def load_trading_config() -> dict:
    data = dict(_DEFAULTS)
    try:
        if _CFG_PATH.exists():
            raw = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
    except Exception as e:
        tlog(f"config load: {e}", "warning")
    data["auto_trade"] = bool(data.get("auto_trade", True))
    data["session_24h"] = bool(data.get("session_24h", True))
    if not data.get("symbols"):
        data["symbols"] = ["EURUSD"]
    return data


def save_trading_config(updates: dict) -> dict:
    data = load_trading_config()
    data.update(updates)
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CFG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def is_paused() -> bool:
    if _runtime_paused:
        return True
    return not bool(load_trading_config().get("auto_trade", True))


def set_paused(paused: bool, persist: bool = True) -> None:
    global _runtime_paused
    _runtime_paused = bool(paused)
    if persist:
        save_trading_config({"auto_trade": not paused})
    tlog(f"CTRL pause={paused} persist={persist}")
    log_decision("control", action="pause" if paused else "resume", persist=persist)


def _parse_hhmm(raw: str) -> tuple[int, int]:
    parts = (raw or "07:00").strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return max(0, min(23, h)), max(0, min(59, m))
    except Exception:
        return 7, 0


def session_open(cfg: dict | None = None, now: datetime | None = None) -> tuple[bool, str]:
    cfg = cfg or load_trading_config()
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False, "weekend"
    if bool(cfg.get("session_24h")):
        return True, "24h weekdays"
    sh, sm = _parse_hhmm(str(cfg.get("session_start_utc") or "07:00"))
    eh, em = _parse_hhmm(str(cfg.get("session_end_utc") or "21:00"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start == end:
        return True, "open"
    if start < end:
        if start <= now < end:
            return True, "open"
        return False, f"outside {sh:02d}:{sm:02d}-{eh:02d}:{em:02d} UTC"
    if now >= start or now < end:
        return True, "open"
    return False, f"outside {sh:02d}:{sm:02d}-{eh:02d}:{em:02d} UTC"


def _session_label(cfg: dict, sess_ok: bool, sess_why: str) -> str:
    if sess_ok and (sess_why == "24h weekdays" or bool(cfg.get("session_24h"))):
        return "24h weekdays"
    if sess_ok:
        return "open"
    return sess_why or "closed"


def _et_to_utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    try:
        et = ZoneInfo("America/New_York")
        return datetime(year, month, day, hour, minute, tzinfo=et).astimezone(timezone.utc)
    except Exception:
        # Fallback: assume EDT (-4)
        return datetime(year, month, day, hour + 4, minute, tzinfo=timezone.utc)


def _first_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def _static_high_impact(now: datetime) -> list[dict]:
    """NFP (first Friday 8:30 ET), plus FOMC/CPI/GDP/ECB UTC stamps for this year."""
    events: list[dict] = []
    year = now.year
    for month in range(1, 13):
        fr = _first_friday(year, month)
        nfp = _et_to_utc(year, month, fr.day, 8, 30)
        events.append({"time": nfp, "level": "high", "currency": "USD", "name": "NFP"})

    # 2026 FOMC statement 14:00 ET (last day of meeting) — typical schedule
    fomc_days = (
        (1, 28), (3, 18), (4, 29), (6, 17),
        (7, 29), (9, 16), (11, 4), (12, 9),
    )
    if year == 2026:
        for mo, day in fomc_days:
            events.append({
                "time": _et_to_utc(year, mo, day, 14, 0),
                "level": "high",
                "currency": "USD",
                "name": "FOMC",
            })
        # USD CPI 8:30 ET — mid-month prints commonly used in calendars
        cpi_days = (14, 11, 11, 10, 12, 10, 14, 12, 11, 13, 10, 10)
        for mo, day in enumerate(cpi_days, start=1):
            events.append({
                "time": _et_to_utc(year, mo, day, 8, 30),
                "level": "high",
                "currency": "USD",
                "name": "CPI",
            })
        gdp_days = ((1, 29), (4, 30), (7, 30), (10, 29))
        for mo, day in gdp_days:
            events.append({
                "time": _et_to_utc(year, mo, day, 8, 30),
                "level": "high",
                "currency": "USD",
                "name": "GDP",
            })
        # ECB ~12:15 UTC typical
        for mo, day in ((1, 22), (3, 12), (4, 30), (6, 11), (7, 23), (9, 10), (10, 29), (12, 17)):
            events.append({
                "time": datetime(year, mo, day, 12, 15, tzinfo=timezone.utc),
                "level": "high",
                "currency": "EUR",
                "name": "ECB rate",
            })
        for mo, day in ((2, 5), (3, 19), (5, 7), (6, 18), (8, 6), (9, 17), (11, 5), (12, 17)):
            events.append({
                "time": datetime(year, mo, day, 12, 0, tzinfo=timezone.utc),
                "level": "high",
                "currency": "GBP",
                "name": "BOE rate",
            })
    return events


def _in_window(event_t: datetime, now: datetime, before_min: int, after_min: int) -> bool:
    if event_t.tzinfo is None:
        event_t = event_t.replace(tzinfo=timezone.utc)
    start = event_t - timedelta(minutes=before_min)
    end = event_t + timedelta(minutes=after_min)
    return start <= now <= end


def news_blackout(
    symbol: str,
    cfg: dict | None = None,
    now: datetime | None = None,
    fa_text: str = "",
) -> tuple[bool, str]:
    cfg = cfg or load_trading_config()
    now = now or datetime.now(timezone.utc)
    before = int(cfg.get("news_blackout_before_min") or 30)
    after = int(cfg.get("news_blackout_after_min") or 15)
    curs = set(c.upper() for c in pair_currencies(symbol))
    if not curs:
        curs = {"USD"}

    hits: list[str] = []
    for ev in calendar_events(list(curs), hours=6):
        if str(ev.get("level") or "").lower() != "high":
            continue
        cur = str(ev.get("currency") or "").upper()
        if cur and cur not in curs:
            continue
        t = ev.get("time")
        if isinstance(t, datetime) and _in_window(t, now, before, after):
            hits.append(f"{cur} {ev.get('name')} {t.strftime('%H:%M')}Z")

    for ev in _static_high_impact(now):
        cur = str(ev.get("currency") or "").upper()
        if cur not in curs:
            continue
        t = ev["time"]
        if _in_window(t, now, before, after):
            hits.append(f"{cur} {ev.get('name')} {t.strftime('%H:%M')}Z")

    if hits:
        return True, hits[0]

    low = (fa_text or "").lower()
    if low:
        named = any(k in low for k in _NEWS_KEYS)
        timed = bool(_FA_TIME.search(low))
        if timed and named:
            return True, "FA headlines name a timed high-impact print"
    return False, ""


def _journal_append(entry: dict) -> None:
    _JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows: list = []
    if _JOURNAL_PATH.exists():
        try:
            rows = json.loads(_JOURNAL_PATH.read_text(encoding="utf-8")) or []
        except Exception:
            rows = []
    if not isinstance(rows, list):
        rows = []
    rows.append(entry)
    rows = rows[-800:]
    _JOURNAL_PATH.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")


def _fmt(price: float, digits: int) -> str:
    return f"{price:.{max(0, int(digits))}f}"


def _stops_from_atr(metrics: dict, side: str, cfg: dict) -> tuple[float, float] | tuple[None, None]:
    atr = float(metrics.get("atr") or 0)
    if atr <= 0:
        return None, None
    point = float(metrics.get("point") or 0.00001)
    stops_level = int(metrics.get("stops_level") or 0)
    freeze = int(metrics.get("freeze_level") or 0)
    min_dist = max(stops_level, freeze) * point
    sl_dist = max(float(cfg.get("sl_atr") or 1.5) * atr, min_dist, point * 10)
    tp_dist = max(float(cfg.get("tp_atr") or 2.0) * atr, min_dist, point * 10)
    digits = int(metrics.get("digits") or 5)
    tick_size = float(metrics.get("tick_size") or point)
    if side == "BUY":
        price = float(metrics.get("ask") or metrics.get("close"))
        sl = price - sl_dist
        tp = price + tp_dist
    else:
        price = float(metrics.get("bid") or metrics.get("close"))
        sl = price + sl_dist
        tp = price - tp_dist

    def _n(p: float) -> float:
        if tick_size > 0:
            p = round(p / tick_size) * tick_size
        return round(p, digits)

    return _n(sl), _n(tp)


def _risk_block(cfg: dict, metrics: dict, symbol: str) -> str | None:
    demo, reason = is_demo_account()
    if not demo:
        return reason or "not demo"
    if is_paused():
        return "auto_trade off"
    from actions.mt5_analysis import ensure_algo_trading
    ok_at, at_why = ensure_algo_trading()
    if not ok_at:
        return at_why
    acc = account_snapshot()
    if not acc.get("ok"):
        return str(acc.get("error") or "no account")
    equity = float(acc.get("equity") or 0)
    magic = int(cfg.get("magic") or MAGIC_DEFAULT)
    pnl = daily_pnl(magic)
    cap = float(cfg.get("daily_loss_pct") or 3.0) / 100.0
    if equity > 0 and pnl <= -cap * equity:
        return f"daily loss {pnl:.2f} hit {cap * 100:.1f}% of equity"
    open_n = len(magic_positions(magic))
    if open_n >= int(cfg.get("max_positions") or 3):
        return f"max open Athena positions ({open_n})"
    spread = float(metrics.get("spread") or 0)
    point = float(metrics.get("point") or 0.00001)
    spread_pts = spread / point if point else 0
    max_pts = float(cfg.get("max_spread_points") or 25)
    if spread_pts > max_pts:
        return f"wide spread {spread_pts:.1f} pts > {max_pts}"
    return None


def _reuse_assessment(symbol: str, tf: str, bar_time: int):
    try:
        from agents.state import get_last
        last = get_last()
    except Exception:
        return None
    if last is None:
        return None
    if str(last.symbol or "").upper() != str(symbol).upper():
        return None
    if str(last.timeframe or "").upper() != str(tf).upper():
        return None
    if int(last.bar_time or 0) != int(bar_time or 0):
        return None
    if last.usable_technical() is None:
        return None
    return last


def _agent_trade_decision(symbol: str, tf: str, bar_time: int):
    """Run or reuse multi-agent analysis. Returns (assessment, side)."""
    from agents.conflict import trade_side

    reused = _reuse_assessment(symbol, tf, bar_time)
    if reused is not None:
        tlog(f"DESK reuse analysis {symbol} {tf} bar={bar_time} overall={reused.overall}")
        return reused, trade_side(reused)
    from agents.orchestrator import run_market_analysis
    from core.activity_logger import activity
    activity(f"Auto-trade is waiting on agent analysis for {symbol} {tf}.")
    assessment = run_market_analysis(symbol, tf)
    return assessment, trade_side(assessment)


def hud_text(cfg: dict | None = None) -> str:
    cfg = cfg or load_trading_config()
    acc = account_snapshot()
    magic = int(cfg.get("magic") or MAGIC_DEFAULT)
    lines = ["ATHENA TRADING DESK"]
    if not acc.get("ok"):
        lines.append(str(acc.get("error") or "MT5 not connected"))
        return "\n".join(lines)
    kind = "demo" if acc.get("demo") else "LIVE — orders refused"
    pnl = daily_pnl(magic)
    auto = "PAUSED" if is_paused() else "ON"
    sess_ok, sess_why = session_open(cfg)
    sess_disp = _session_label(cfg, sess_ok, sess_why)
    lines.append(
        f"ACCOUNT  {kind}  login={acc.get('login')}  {acc.get('server')}  "
        f"equity={acc.get('equity'):.2f} {acc.get('currency')}  "
        f"daily P&L={pnl:+.2f}"
    )
    try:
        from actions.mt5_analysis import mt5_connection_status
        st = mt5_connection_status()
        ka = "keepalive on" if st.get("keepalive") else "keepalive off"
        lines.append(
            f"MT5  {'UP' if st.get('ok') else 'DOWN'}  "
            f"AutoTrading={'ON' if st.get('trade_allowed') else 'OFF'}  "
            f"{ka}  {st.get('reason')}"
        )
    except Exception:
        pass
    lines.append(
        f"AUTO-TRADE  {auto}  source=agents (TA leads)  session={sess_disp}  "
        f"last bias={_last_bias} last={_last_block or '-'}"
    )
    pos = magic_positions(magic)
    if not pos:
        lines.append("POSITIONS  none")
    else:
        lines.append("POSITIONS")
        for p in pos:
            lines.append(
                f"  {p['symbol']}  {p['side']}  {p['volume']}  "
                f"open={p['open']}  SL={p['sl']}  TP={p['tp']}  "
                f"P&L={p['profit']:+.2f}"
            )
    if _last_brief:
        lines.append("LAST")
        if _last_brief.get("status"):
            lines.append(
                f"  {_last_brief.get('status')}  bias={_last_brief.get('bias') or _last_bias}  "
                f"score={_last_brief.get('score') or _last_score}"
            )
        if _last_brief.get("why"):
            lines.append(f"  {_last_brief['why'][:220]}")
        if _last_brief.get("plan"):
            lines.append(f"  {_last_brief['plan'][:180]}")
        if _last_brief.get("gates"):
            lines.append(f"  {_last_brief['gates'][:180]}")
        if _last_brief.get("reason"):
            lines.append(f"  {_last_brief['reason'][:180]}")
    try:
        from agents.state import format_hud_analysis
        extra = format_hud_analysis()
        if extra:
            lines.append(extra)
    except Exception:
        pass
    return "\n".join(lines)


def refresh_hud(player=None, cfg: dict | None = None) -> str:
    text = hud_text(cfg)
    if player is not None and hasattr(player, "show_content"):
        try:
            player.show_content("TRADING", text, nowrap=True)
        except Exception:
            pass
    return text


def _card(
    *,
    status: str,
    bias: str,
    symbol: str,
    extra: str = "",
    metrics: dict | None = None,
    exec_result: dict | None = None,
    why: str = "",
    plan: str = "",
    gates: str = "",
) -> str:
    m = metrics or {}
    d = int(m.get("digits") or 5)
    parts = [
        f"STATUS {status}",
        f"BIAS {bias}",
        f"SYMBOL {symbol}  TF {m.get('tf') or ''}".strip(),
    ]
    if m.get("ok"):
        parts.append(
            f"PRICE {_fmt(float(m.get('close') or 0), d)}  "
            f"ATR {_fmt(float(m.get('atr') or 0), d)}  RSI {float(m.get('rsi') or 0):.1f}"
        )
        parts.append("REASONS: " + "; ".join(m.get("reasons") or []))
    if why:
        parts.append(why)
    if plan:
        parts.append(plan)
    if gates:
        parts.append(gates)
    if extra:
        parts.append(extra)
    if exec_result:
        parts.append(
            f"EXEC {exec_result.get('status')}  "
            f"{exec_result.get('reason') or ''}  "
            f"ticket={exec_result.get('ticket', '-')}"
        )
    speak = {
        "BUY": f"Bias is BUY on {symbol}.",
        "SELL": f"Bias is SELL on {symbol}.",
        "WAIT": f"Bias is WAIT on {symbol} — no new order.",
    }.get(bias, f"Bias is {bias}.")
    parts.append(
        f"SAY ALOUD: {speak} "
        "In a few sentences name the two or three strongest REASONS, "
        "then the PLAN (size, SL, TP) if it is not none, "
        "then the outcome word from STATUS: FILLED and the ticket, "
        "WAIT, or BLOCKED and the reason. "
        "Never invent a fill, price, or ticket. Do not narrate tool calls."
    )
    return "\n".join(parts)


def _ta_snapshot(metrics: dict) -> dict[str, Any]:
    keys = (
        "ok", "symbol", "tf", "close", "bid", "ask", "spread",
        "ema20", "ema50", "ema200", "trend", "rsi", "macd_hist", "atr",
        "bb_u", "bb_l", "bb_pos", "support", "resistance", "candle",
        "signal", "conf", "reasons", "digits", "bar_time",
    )
    return {k: metrics.get(k) for k in keys}


def _calendar_snapshot(events: list) -> list[dict]:
    out: list[dict] = []
    for e in (events or [])[:8]:
        t = e.get("time")
        out.append({
            "currency": e.get("currency"),
            "name": e.get("name"),
            "level": e.get("level"),
            "time": t.isoformat() if isinstance(t, datetime) else str(t or ""),
        })
    return out


def trading_desk(parameters: dict | None = None, player=None, **_kw) -> str:
    """Agent analysis first (TA leads), then demo order if side is BUY/SELL and gates pass."""
    global _last_bias, _last_block, _last_score, _last_status, _last_brief
    args = parameters or {}
    cfg = load_trading_config()
    symbol = str(args.get("symbol") or args.get("pair") or "").strip()
    if not symbol:
        symbol = str((cfg.get("symbols") or ["EURUSD"])[0])
    from actions.mt5_analysis import _norm_symbol
    symbol = _norm_symbol(symbol)
    tf = str(args.get("timeframe") or args.get("tf") or cfg.get("timeframe") or "H1")
    force_analyze = bool(args.get("analyze_only") or False)
    tlog(f"DESK start {symbol} {tf} analyze_only={force_analyze} source=agents")

    metrics = get_ta_metrics(symbol, tf)
    if not metrics.get("ok"):
        with _lock:
            _last_block = str(metrics.get("error") or "TA failed")
            _last_status = "BLOCKED"
            _last_brief = {
                "status": "BLOCKED",
                "bias": "WAIT",
                "score": "0",
                "why": f"WHY TA failed: {_last_block}",
                "plan": "PLAN none",
                "gates": "GATES session=- news=- risk=ta failed",
                "reason": _last_block,
            }
        log_decision(
            "desk", status="BLOCKED", symbol=symbol, bias="WAIT",
            reason=str(metrics.get("error") or "TA failed"),
        )
        text = _card(
            status="BLOCKED",
            bias="WAIT",
            symbol=symbol,
            extra=f"BLOCKED ta: {metrics.get('error')}",
            metrics=metrics,
            why=f"WHY TA failed: {metrics.get('error')}",
            plan="PLAN none",
            gates="GATES session=- news=- risk=ta failed",
        )
        refresh_hud(player, cfg)
        return text

    engine_score = int(metrics.get("score") or 0)
    bar_time = int(metrics.get("bar_time") or 0)
    digits = int(metrics.get("digits") or 5)
    vol = float(cfg.get("volume") or 0.01)
    sl_atr = float(cfg.get("sl_atr") or 1.5)
    tp_atr = float(cfg.get("tp_atr") or 2.0)
    tlog(
        f"TA engine {symbol} {tf} score={engine_score} "
        f"rsi={float(metrics.get('rsi') or 0):.1f} "
        f"atr={metrics.get('atr')} spread={metrics.get('spread')} "
        f"bar={bar_time} reasons={'; '.join(metrics.get('reasons') or [])}"
    )

    assessment = None
    bias = "WAIT"
    agent_overall = ""
    agent_conf = 0.0
    if not force_analyze:
        try:
            # Outside desk lock — full agent run can take minutes.
            assessment, bias = _agent_trade_decision(symbol, tf, bar_time)
            agent_overall = str(getattr(assessment, "overall", "") or "")
            agent_conf = float(getattr(assessment, "confidence", 0) or 0)
        except RuntimeError as e:
            with _lock:
                _last_bias = "WAIT"
                _last_block = str(e)
                _last_status = "WAIT"
            refresh_hud(player, cfg)
            return _card(
                status="WAIT",
                bias="WAIT",
                symbol=symbol,
                extra=f"WAIT agents busy: {e}",
                metrics=metrics,
                why="WHY agent analysis already running",
                plan="PLAN none",
                gates="GATES deferred",
            )
        except Exception as e:
            tlog(f"agent decision failed: {e}", "error")
            with _lock:
                _last_bias = "WAIT"
                _last_block = f"agent analysis failed: {e}"
                _last_status = "BLOCKED"
            refresh_hud(player, cfg)
            return _card(
                status="BLOCKED",
                bias="WAIT",
                symbol=symbol,
                extra=f"BLOCKED agents: {e}",
                metrics=metrics,
                why=f"WHY agent analysis failed: {e}",
                plan="PLAN none",
                gates="GATES agents failed",
            )
    if bias not in ("BUY", "SELL", "WAIT"):
        bias = "WAIT"

    with _lock:
        _last_score = engine_score
        _last_bias = bias

        fa = news_headlines(symbol)
        cal = calendar_events(pair_currencies(symbol), hours=48)
        cal_line = ""
        if cal:
            cal_line = "CALENDAR: " + "; ".join(
                f"{e.get('currency')} {e.get('name')} {e['time'].strftime('%b %d %H:%M')}Z"
                for e in cal[:5] if isinstance(e.get("time"), datetime)
            )
        else:
            cal_line = "CALENDAR not in this MT5 API"

        plan_sl = plan_tp = None
        plan_entry = None
        plan_dict: dict[str, Any] = {}
        if bias in ("BUY", "SELL"):
            plan_sl, plan_tp = _stops_from_atr(metrics, bias, cfg)
            plan_entry = float(
                (metrics.get("ask") if bias == "BUY" else metrics.get("bid"))
                or metrics.get("close")
                or 0
            )
            if plan_sl is not None and plan_tp is not None:
                plan_dict = {
                    "side": bias,
                    "volume": vol,
                    "entry": plan_entry,
                    "sl": plan_sl,
                    "tp": plan_tp,
                    "sl_atr": sl_atr,
                    "tp_atr": tp_atr,
                }
                plan_line = (
                    f"PLAN {bias} {symbol} vol={vol} "
                    f"entry~{_fmt(plan_entry, digits)} "
                    f"SL={_fmt(plan_sl, digits)} TP={_fmt(plan_tp, digits)} "
                    f"({sl_atr} / {tp_atr} ATR)"
                )
            else:
                plan_line = "PLAN none"
        else:
            plan_line = "PLAN none"

        if assessment is not None:
            why_line = (
                f"WHY agents overall={agent_overall} conf={agent_conf:.0%} "
                f"engine_score={engine_score}; {(assessment.why or '')[:180]}"
            )
        else:
            why_line = (
                f"WHY engine_score={engine_score}; "
                + "; ".join(metrics.get("reasons") or [])
            )

        sess_ok, sess_why = session_open(cfg)
        blocked_news, news_why = news_blackout(symbol, cfg, fa_text=fa)
        risk = _risk_block(cfg, metrics, symbol)
        autotrading_on = True
        try:
            from actions.mt5_analysis import mt5_connection_status
            autotrading_on = bool(mt5_connection_status().get("trade_allowed"))
        except Exception:
            pass
        demo_ok, _demo_why = is_demo_account()
        sess_disp = _session_label(cfg, sess_ok, sess_why)
        news_disp = "ok" if not blocked_news else news_why
        risk_disp = "ok" if not risk else risk
        gates_line = (
            f"GATES session={sess_disp} news={news_disp} "
            f"risk={risk_disp} AutoTrading={'ON' if autotrading_on else 'OFF'}"
        )
        extra_bits = [
            f"AGENTS {agent_overall or '-'} conf={agent_conf:.0%}" if assessment else "AGENTS skipped",
            f"ENGINE score={engine_score}",
            cal_line,
            (fa or "")[:400],
        ]

        def _done(st: str, reason: str, executed=None) -> str:
            global _last_block, _last_status, _last_brief
            _last_block = reason
            _last_status = st
            extra_bits.append(f"{st} {reason}".strip())
            card = _card(
                status=st,
                bias=bias,
                symbol=symbol,
                extra="\n".join(x for x in extra_bits if x),
                metrics=metrics,
                exec_result=executed,
                why=why_line,
                plan=plan_line,
                gates=gates_line,
            )
            _last_brief = {
                "status": st,
                "bias": bias,
                "score": str(engine_score),
                "why": why_line,
                "plan": plan_line,
                "gates": gates_line,
                "reason": reason,
                "overall": agent_overall,
                "confidence": f"{agent_conf:.0%}",
            }
            acc = account_snapshot()
            _journal_append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "tf": tf,
                "bias": bias,
                "status": st,
                "reason": reason,
                "bar_time": bar_time,
                "score": engine_score,
                "overall": agent_overall,
                "confidence": agent_conf,
                "rsi": metrics.get("rsi"),
                "atr": metrics.get("atr"),
                "close": metrics.get("close"),
                "plan": plan_dict or None,
                "exec": executed,
            })
            log_decision(
                "desk",
                status=st,
                symbol=symbol,
                tf=tf,
                bias=bias,
                score=engine_score,
                overall=agent_overall,
                confidence=agent_conf,
                reason=reason,
                bar_time=bar_time,
                ticket=(executed or {}).get("ticket") if executed else None,
            )
            log_analysis({
                "symbol": symbol,
                "tf": tf,
                "bar_time": bar_time,
                "status": st,
                "bias": bias,
                "score": engine_score,
                "overall": agent_overall,
                "confidence": agent_conf,
                "ta": _ta_snapshot(metrics),
                "fa": {
                    "headlines": (fa or "")[:800],
                    "calendar": _calendar_snapshot(cal),
                    "news_blackout": bool(blocked_news),
                    "news_why": news_why or "",
                },
                "gates": {
                    "session": sess_disp,
                    "news": news_disp,
                    "risk": risk_disp,
                    "pause": is_paused(),
                    "autotrading": autotrading_on,
                    "demo": bool(demo_ok),
                },
                "plan": plan_dict or None,
                "exec": executed,
                "account": {
                    "login": acc.get("login"),
                    "equity": acc.get("equity"),
                    "daily_pnl": daily_pnl(int(cfg.get("magic") or MAGIC_DEFAULT)),
                },
                "reason": reason,
            })
            refresh_hud(player, cfg)
            return card

        if force_analyze:
            return _done("WAIT", "analyze_only")

        if not sess_ok:
            return _done("BLOCKED", f"session {sess_why}")
        if blocked_news:
            return _done("BLOCKED", f"news {news_why}")

        if risk:
            return _done("BLOCKED", risk)

        if bias == "WAIT":
            detail = agent_overall or f"engine_score={engine_score}"
            return _done("WAIT", f"agents say WAIT ({detail})")

        prev = _last_fire.get(symbol)
        if prev == (bar_time, bias):
            return _done("BLOCKED", f"idempotent {symbol} bar={bar_time} {bias}")

        if plan_sl is None or plan_tp is None:
            return _done("BLOCKED", "ATR/stops unavailable")

        tlog(
            f"EXEC agents {bias} {symbol} overall={agent_overall} "
            f"conf={agent_conf:.0%} sl={plan_sl} tp={plan_tp} vol={vol}"
        )
        exec_result = place_market(
            symbol=symbol,
            side=bias,
            volume=vol,
            sl=plan_sl,
            tp=plan_tp,
            magic=int(cfg.get("magic") or MAGIC_DEFAULT),
            deviation=int(cfg.get("deviation") or 20),
            comment=str(cfg.get("comment") or "Athena"),
        )
        st = str(exec_result.get("status") or "BLOCKED")
        if st in ("FILLED", "CLOSED"):
            _last_fire[symbol] = (bar_time, bias)
        reason = str(exec_result.get("reason") or st)
        if st == "FILLED":
            reason = (
                f"{bias} {symbol} vol={exec_result.get('volume')} "
                f"@ {exec_result.get('price')} SL={exec_result.get('sl')} "
                f"TP={exec_result.get('tp')} ticket={exec_result.get('ticket')} "
                f"(agents {agent_overall} {agent_conf:.0%})"
            )
        log_decision(
            "exec",
            status=st,
            symbol=symbol,
            side=bias,
            ticket=exec_result.get("ticket"),
            reason=reason,
            price=exec_result.get("price"),
            sl=exec_result.get("sl"),
            tp=exec_result.get("tp"),
            overall=agent_overall,
            confidence=agent_conf,
        )
        return _done(st, reason, exec_result)


def trading_control(parameters: dict | None = None, player=None, **_kw) -> str:
    args = parameters or {}
    action = str(args.get("action") or "status").strip().lower().replace("-", "_")
    cfg = load_trading_config()
    magic = int(cfg.get("magic") or MAGIC_DEFAULT)

    if action in ("pause", "stop", "off"):
        set_paused(True, persist=True)
        refresh_hud(player, cfg)
        return "AUTO-TRADE PAUSED. Watch-loop will not place new orders. Existing SL/TP stay with the broker."

    if action in ("resume", "start", "on"):
        set_paused(False, persist=True)
        refresh_hud(player, cfg)
        return "AUTO-TRADE ON. Watch-loop will fire on new bars if gates pass."

    if action in ("flatten", "close", "close_all", "flat"):
        result = close_magic(magic=magic)
        log_decision(
            "flatten",
            status=result.get("status"),
            reason=result.get("reason"),
            closed=result.get("closed"),
            tickets=result.get("tickets"),
        )
        refresh_hud(player, cfg)
        _journal_append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": "*",
            "bias": "FLAT",
            "status": result.get("status"),
            "reason": result.get("reason"),
            "exec": result,
        })
        if result.get("ok"):
            return (
                f"CLOSED {result.get('closed', 0)} Athena ticket(s). "
                f"{result.get('reason')}"
            )
        return f"BLOCKED flatten: {result.get('reason')}"

    # status
    refresh_hud(player, cfg)
    acc = account_snapshot()
    pos = magic_positions(magic)
    auto = "PAUSED" if is_paused() else "ON"
    sess_ok, sess_why = session_open(cfg)
    demo = "demo" if acc.get("demo") else "LIVE"
    return (
        f"STATUS auto={auto} account={demo} login={acc.get('login')} "
        f"equity={acc.get('equity')} daily_pnl={daily_pnl(magic):+.2f} "
        f"session={_session_label(cfg, sess_ok, sess_why)} "
        f"positions={len(pos)} last_bias={_last_bias} last={_last_block or '-'} "
        f"symbols={','.join(cfg.get('symbols') or [])} tf={cfg.get('timeframe')}"
    )


def _retryable_block(reason: str) -> bool:
    r = (reason or "").lower()
    return any(
        x in r
        for x in (
            "autotrading",
            "10027",
            "trade_allowed",
            "ipc",
            "not connected",
            "could not connect",
        )
    )


def watch_tick(player=None) -> list[str]:
    """Once per new bar: run agent analysis, then trade if agents say BUY/SELL."""
    cfg = load_trading_config()
    if is_paused():
        tlog("WATCH skip — auto_trade paused")
        refresh_hud(player, cfg)
        return []
    tf = str(cfg.get("timeframe") or "H1")
    out: list[str] = []
    for raw in cfg.get("symbols") or ["EURUSD"]:
        from actions.mt5_analysis import _norm_symbol
        symbol = _norm_symbol(str(raw))
        metrics = get_ta_metrics(symbol, tf)
        if not metrics.get("ok"):
            tlog(f"WATCH {symbol} TA fail: {metrics.get('error')}", "warning")
            continue
        bar = int(metrics.get("bar_time") or 0)
        score = int(metrics.get("score") or 0)
        if not bar:
            tlog(f"WATCH {symbol} skip — no bar_time", "warning")
            continue
        # Reuse an agent assessment already finished for this bar.
        reused = _reuse_assessment(symbol, tf, bar)
        if reused is not None:
            from agents.conflict import trade_side
            side = trade_side(reused)
            prev = _last_eval.get(symbol)
            if prev and prev[0] == bar and prev[2] in ("FILLED", "WAIT", "CLOSED"):
                tlog(
                    f"WATCH {symbol} skip same bar agents bar={bar} "
                    f"side={side} {prev[2]} overall={reused.overall}",
                    "debug",
                )
                continue
            if prev and prev[0] == bar and prev[2] == "BLOCKED" and not _retryable_block(_last_block):
                tlog(
                    f"WATCH {symbol} skip BLOCKED bar={bar} {_last_block}",
                    "debug",
                )
                continue
        else:
            prev = _last_eval.get(symbol)
            if prev and prev[0] == bar and prev[2] in ("FILLED", "WAIT", "CLOSED", "BLOCKED"):
                if prev[2] != "BLOCKED" or not _retryable_block(_last_block):
                    tlog(
                        f"WATCH {symbol} skip already evaluated bar={bar} {prev[2]}",
                        "debug",
                    )
                    continue

        tlog(f"WATCH fire agents {symbol} bar={bar} engine_score={score} prev={_last_eval.get(symbol)}")
        card = trading_desk({"symbol": symbol, "timeframe": tf}, player=player)
        st = ""
        bias = _last_bias or "WAIT"
        for line in card.splitlines():
            if line.startswith("STATUS "):
                st = line.split(maxsplit=1)[-1].strip().upper()
            elif line.startswith("BIAS "):
                bias = line.split(maxsplit=1)[-1].strip().upper()
        _last_eval[symbol] = (bar, bias, st or "WAIT")
        if st in ("FILLED", "CLOSED", "BLOCKED", "WAIT"):
            if player is not None and hasattr(player, "write_log"):
                try:
                    player.write_log(
                        f"SYS: DESK {st} {symbol} {bias} agents "
                        f"(engine_score={score})"
                    )
                except Exception:
                    pass
        skip_speak = (
            "already in" in card.lower()
            or "idempotent" in card.lower()
            or "agents busy" in card.lower()
        )
        if not skip_speak:
            out.append(card)
    refresh_hud(player, cfg)
    return out
