"""
Read-only MetaTrader 5 analysis: quotes, deterministic TA, calendar FA, one chart snapshot.
Never places, modifies, or closes orders.
"""
from __future__ import annotations

import io
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore


_BARS = 260
_CACHE_TTL = 20.0
_NEWS_TIMEOUT = 2.5
_JPEG_Q = 90
_SNAP_MAX = (1920, 1080)
_KEEPALIVE_SEC = 10.0
_RECONNECT_TRIES = 4

_lock = threading.RLock()
_initialized = False
_ever_connected = False
_keepalive_thread: threading.Thread | None = None
_keepalive_stop = threading.Event()
_last_health = ""
_fail_streak = 0
_rates_cache: dict[tuple[str, int], tuple[float, np.ndarray]] = {}

_TF_MAP = {
    "m1": "TIMEFRAME_M1",
    "m5": "TIMEFRAME_M5",
    "m15": "TIMEFRAME_M15",
    "m30": "TIMEFRAME_M30",
    "h1": "TIMEFRAME_H1",
    "h4": "TIMEFRAME_H4",
    "d1": "TIMEFRAME_D1",
    "w1": "TIMEFRAME_W1",
}

CHART_ANALYSIS_PROMPT = (
    "This is a ONE-SHOT snapshot of the user's MetaTrader 5 chart "
    "(or the primary monitor if the MT5 window was not found). "
    "Do technical analysis of what is VISIBLE: instrument, timeframe, trend, "
    "structure, support/resistance, indicators on the panes, notable candles. "
    "Do not invent numbers that are not readable. "
    "Then give a clear directional call: BUY, SELL, or WAIT. "
    "If the user asked should I buy or sell, you MUST answer with one of those three words. "
    "Never say you cannot give trading advice. Never call share_screen. "
    "If you can read a symbol, you MAY then call mt5_analysis action=ta or "
    "analyze once and speak that result's BIAS line as the final call. "
    "You are not placing an order in MetaTrader."
)


def _log(msg: str) -> None:
    print(f"[MT5] {msg}")
    try:
        from core.mt5_log import mt5_log
        mt5_log(msg, "debug")
    except Exception:
        pass


def _last_error() -> tuple[int, str]:
    if mt5 is None:
        return (-1, "package missing")
    try:
        err = mt5.last_error()
    except Exception as e:
        return (-1, str(e))
    if err is None:
        return (0, "")
    try:
        code, text = int(err[0]), str(err[1] if len(err) > 1 else err)
        return code, text
    except Exception:
        return (0, str(err))


def _is_ipc_error(code: int) -> bool:
    return int(code) <= -10000


def _running_terminal_path() -> str | None:
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            n = (p.info.get("name") or "").lower()
            if n in ("terminal64.exe", "terminal.exe"):
                exe = p.info.get("exe")
                if exe:
                    return str(exe)
    except Exception:
        pass
    return None


def _teardown() -> None:
    global _initialized, _last_health
    _initialized = False
    _last_health = ""
    _rates_cache.clear()
    try:
        mt5.shutdown()
    except Exception:
        pass


def _health() -> tuple[bool, str]:
    """True only if IPC is up AND the terminal is connected to a broker."""
    if mt5 is None:
        return False, "MetaTrader5 package not installed"
    try:
        term = mt5.terminal_info()
    except Exception as e:
        return False, f"terminal_info exception: {e} last={_last_error()}"
    if term is None:
        code, text = _last_error()
        return False, f"terminal_info=None last_error=({code}, {text})"
    connected = bool(getattr(term, "connected", False))
    if not connected:
        code, text = _last_error()
        return False, (
            f"broker disconnected name={getattr(term, 'name', '')} "
            f"last_error=({code}, {text})"
        )
    try:
        acc = mt5.account_info()
    except Exception as e:
        return False, f"account_info exception: {e}"
    if acc is None:
        code, text = _last_error()
        return False, f"account_info=None last_error=({code}, {text})"
    return True, (
        f"login={acc.login} server={acc.server} "
        f"connected=1 trade_allowed={bool(getattr(term, 'trade_allowed', False))}"
    )


def _attach() -> bool:
    path = _running_terminal_path()
    kwargs: dict = {"timeout": 10_000}
    if path:
        kwargs["path"] = path
    try:
        return bool(mt5.initialize(**kwargs))
    except TypeError:
        try:
            return bool(mt5.initialize(path) if path else mt5.initialize())
        except Exception:
            return False
    except Exception:
        return False


def _ensure_mt5() -> str | None:
    """Keep a live IPC to the running terminal. Reconnects on drop."""
    global _initialized, _last_health, _fail_streak, _ever_connected
    if mt5 is None:
        return (
            "MetaTrader5 Python package is not installed. "
            "Run: pip install MetaTrader5"
        )

    with _lock:
        if _initialized:
            ok, why = _health()
            if ok:
                if why != _last_health:
                    from core.mt5_log import mt5_event
                    mt5_event("ok", status="healthy", reason=why)
                    _last_health = why
                _fail_streak = 0
                start_mt5_keepalive()
                return None
            from core.mt5_log import mt5_event
            mt5_event("drop", status="unhealthy", reason=why)
            _log(f"connection dropped: {why}")
            _teardown()

    last_why = ""
    for attempt in range(1, _RECONNECT_TRIES + 1):
        if attempt > 1:
            time.sleep(0.35 * attempt)
        with _lock:
            attached = _attach()
            code, text = _last_error()
            if not attached:
                last_why = f"initialize failed attempt={attempt} last_error=({code}, {text})"
                from core.mt5_log import mt5_event
                mt5_event("fail", attempt=attempt, error=f"({code}, {text})")
                _teardown()
                continue
            ok, why = _health()
            if ok:
                _initialized = True
                _fail_streak = 0
                _last_health = why
                kind = "reconnect" if _ever_connected else "connect"
                _ever_connected = True
                from core.mt5_log import mt5_event
                mt5_event(kind, status="connected", reason=why, attempt=attempt)
                _log(f"{kind} ({why})")
                start_mt5_keepalive()
                return None
            last_why = why
            from core.mt5_log import mt5_event
            mt5_event("fail", attempt=attempt, error=why)
            _teardown()

    _fail_streak += 1
    return (
        "Could not connect to MetaTrader 5. "
        "Open the terminal, log in, and try again. "
        f"({last_why})"
    )


def recover_mt5(reason: str = "") -> str | None:
    """Force IPC teardown + reconnect (IPC timeout / None ticks)."""
    from core.mt5_log import mt5_event
    mt5_event("ipc", reason=reason or "forced recover")
    with _lock:
        _teardown()
    return _ensure_mt5()


def start_mt5_keepalive(interval: float = _KEEPALIVE_SEC) -> None:
    """Background ping so the Python IPC does not go idle and die."""
    global _keepalive_thread
    if mt5 is None:
        return
    with _lock:
        t = _keepalive_thread
        if t is not None and t.is_alive():
            return
        _keepalive_stop.clear()

        def _loop():
            from core.mt5_log import mt5_log
            mt5_log(f"keepalive started interval={interval}s")
            backoff = interval
            while True:
                err = _ensure_mt5()
                if err:
                    backoff = min(30.0, max(interval, backoff * 1.5))
                    mt5_log(f"keepalive reconnect pending: {err}", "warning")
                else:
                    backoff = interval
                if _keepalive_stop.wait(backoff):
                    mt5_log("keepalive stopped")
                    return

        _keepalive_thread = threading.Thread(
            target=_loop, name="mt5-keepalive", daemon=True
        )
        _keepalive_thread.start()


def stop_mt5_keepalive() -> None:
    _keepalive_stop.set()


def mt5_connection_status() -> dict:
    ok, why = _health() if _initialized else (False, "not initialized")
    code, text = _last_error()
    trade_allowed = False
    try:
        term = mt5.terminal_info() if mt5 else None
        trade_allowed = bool(term and getattr(term, "trade_allowed", False))
    except Exception:
        pass
    return {
        "ok": ok,
        "initialized": _initialized,
        "reason": why,
        "trade_allowed": trade_allowed,
        "last_error": (code, text),
        "keepalive": bool(_keepalive_thread and _keepalive_thread.is_alive()),
    }


def _mt5_main_hwnd() -> int | None:
    """Root HWND of the largest visible MT5 terminal window."""
    pids = _mt5_pids()
    best = 0
    best_area = 0
    try:
        import win32gui
        import win32con
        import win32process

        def _cb(hwnd, _):
            nonlocal best, best_area
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.IsIconic(hwnd):
                return True
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = (win32gui.GetWindowText(hwnd) or "").lower()
            by_pid = pid in pids
            by_title = any(k in title for k in ("metatrader", "mt5", "metaquotes"))
            if not by_pid and not by_title:
                return True
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            except Exception:
                return True
            area = max(0, right - left) * max(0, bottom - top)
            if area > best_area:
                best_area = area
                try:
                    best = int(win32gui.GetAncestor(hwnd, win32con.GA_ROOT) or hwnd)
                except Exception:
                    best = int(hwnd)
            return True

        win32gui.EnumWindows(_cb, None)
    except Exception as e:
        _log(f"hwnd enum: {e}")
    return best or None


def algo_trading_on() -> bool:
    err = _ensure_mt5()
    if err or mt5 is None:
        return False
    try:
        term = mt5.terminal_info()
    except Exception:
        return False
    return bool(term and getattr(term, "trade_allowed", False))


def ensure_algo_trading() -> tuple[bool, str]:
    """
    MT5 will reject order_send with 10027 unless the AutoTrading button is on.
    If it is off, toggle it via WM_COMMAND on the terminal window.
    """
    if algo_trading_on():
        return True, "on"
    hwnd = _mt5_main_hwnd()
    if not hwnd:
        return False, (
            "MT5 AutoTrading is OFF (error 10027). "
            "Open MetaTrader 5 and click AutoTrading on the toolbar "
            "(green play / Algo Trading) so it stays enabled."
        )
    try:
        import win32gui
        import win32con
        from core.mt5_log import mt5_event
        # Documented / community IDs for MT4/MT5 AutoTrading toggle
        for cmd in (32851, 33048, 33020, 33135, 35462, 32990):
            try:
                win32gui.PostMessage(hwnd, win32con.WM_COMMAND, cmd, 0)
            except Exception:
                continue
            time.sleep(0.3)
            if algo_trading_on():
                mt5_event("ok", status="autotrading_on", reason=f"WM_COMMAND {cmd}")
                return True, f"enabled via toolbar command {cmd}"
    except Exception as e:
        _log(f"enable AutoTrading: {e}")
    if algo_trading_on():
        return True, "on"
    from core.mt5_log import mt5_event
    mt5_event(
        "fail",
        error="trade_allowed=False",
        reason="AutoTrading disabled by client (10027)",
    )
    return False, (
        "MT5 AutoTrading is OFF — Athena cannot place orders (10027). "
        "In MetaTrader 5 click the AutoTrading button on the top toolbar "
        "until it is green / enabled. Tools → Options → Expert Advisors "
        "must also allow algorithmic trading."
    )


def _timeframe(name: str):
    key = (name or "H1").strip().lower().replace(" ", "")
    attr = _TF_MAP.get(key, "TIMEFRAME_H1")
    return getattr(mt5, attr)


def _norm_symbol(raw: str) -> str:
    s = (raw or "").strip().upper().replace("/", "").replace("-", "").replace(" ", "")
    aliases = {
        "GOLD": "XAUUSD",
        "XAU": "XAUUSD",
        "SILVER": "XAGUSD",
        "XAG": "XAGUSD",
        "USOIL": "USOIL",
        "BRENT": "UKOIL",
    }
    return aliases.get(s, s)


def _pair_currencies(symbol: str) -> list[str]:
    s = _norm_symbol(symbol)
    if len(s) >= 6 and s[:6].isalpha():
        return [s[:3], s[3:6]]
    if s.startswith("XAU"):
        return ["USD"]
    if s.startswith("XAG"):
        return ["USD"]
    return []


def _select(symbol: str) -> str | None:
    info = mt5.symbol_info(symbol)
    if info is None:
        code, text = _last_error()
        if _is_ipc_error(code):
            rec = recover_mt5(f"symbol_info {symbol} ({code}, {text})")
            if rec is None:
                info = mt5.symbol_info(symbol)
        if info is None:
            return f"Unknown symbol: {symbol}. Check the name in Market Watch."
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            return f"Could not select {symbol} in Market Watch."
    return None


def _copy_rates(symbol: str, tf) -> np.ndarray | None:
    tf_int = int(tf)
    key = (symbol, tf_int)
    now = time.monotonic()
    hit = _rates_cache.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, _BARS)
    if rates is None or len(rates) < 30:
        code, text = _last_error()
        if _is_ipc_error(code):
            from core.mt5_log import mt5_event
            mt5_event(
                "ipc",
                reason=f"copy_rates {symbol} last_error=({code}, {text})",
            )
            rec = recover_mt5(f"copy_rates {symbol} ({code}, {text})")
            if rec is None:
                rates = mt5.copy_rates_from_pos(symbol, tf, 0, _BARS)
        if rates is None or len(rates) < 30:
            return None
    arr = np.array(rates)
    _rates_cache[key] = (now, arr)
    return arr


def _ema(close: np.ndarray, period: int) -> np.ndarray:
    s = pd.Series(close, dtype="float64")
    return s.ewm(span=period, adjust=False).mean().to_numpy()


def _rsi(close: np.ndarray, period: int = 14) -> float:
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_g = pd.Series(gain).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    avg_l = pd.Series(loss).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    rs = avg_g[-1] / (avg_l[-1] + 1e-12)
    return float(100 - (100 / (1 + rs)))


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return float(pd.Series(tr).ewm(span=period, adjust=False).mean().iloc[-1])


def _swings(high: np.ndarray, low: np.ndarray, look: int = 3) -> tuple[float, float]:
    n = len(high)
    start = max(look, n - 80)
    sh, sl = [], []
    for i in range(start, n - look):
        if high[i] >= high[i - look:i].max() and high[i] >= high[i + 1:i + 1 + look].max():
            sh.append(float(high[i]))
        if low[i] <= low[i - look:i].min() and low[i] <= low[i + 1:i + 1 + look].min():
            sl.append(float(low[i]))
    res = sh[-1] if sh else float(high[-20:].max())
    sup = sl[-1] if sl else float(low[-20:].min())
    return sup, res


def _candle_note(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray) -> str:
    if len(c) < 3:
        return "none"
    bits = []
    o1, h1, l1, c1 = o[-2], h[-2], l[-2], c[-2]
    o0, h0, l0, c0 = o[-1], h[-1], l[-1], c[-1]
    body0 = abs(c0 - o0)
    wick_up = h0 - max(c0, o0)
    wick_dn = min(c0, o0) - l0
    rng = max(h0 - l0, 1e-12)
    if c0 > o0 and c1 < o1 and c0 >= o1 and o0 <= c1:
        bits.append("bullish engulfing")
    elif c0 < o0 and c1 > o1 and c0 <= o1 and o0 >= c1:
        bits.append("bearish engulfing")
    if wick_dn > 2 * body0 and wick_up < body0 and (min(c0, o0) - l0) / rng > 0.55:
        bits.append("bullish pin")
    if wick_up > 2 * body0 and wick_dn < body0 and (h0 - max(c0, o0)) / rng > 0.55:
        bits.append("bearish pin")
    return ", ".join(bits) if bits else "none"


def _score(close: float, e20: float, e50: float, e200: float, rsi: float,
           macd_hist: float, atr: float, sup: float, res: float, candle: str
           ) -> tuple[int, str, str, float, list[str]]:
    score = 0
    stack = 0
    reasons: list[str] = []
    if close > e20 > e50 > e200:
        score += 2
        stack = 1
        reasons.append("EMA 20>50>200 stack")
    elif close < e20 < e50 < e200:
        score -= 2
        stack = -1
        reasons.append("EMA 20<50<200 stack")
    else:
        reasons.append("EMAs mixed")

    if rsi < 30:
        reasons.append(f"RSI oversold {rsi:.1f}")
        if stack < 0:
            reasons.append("RSI ignored vs EMA stack")
        else:
            score += 1
    elif rsi > 70:
        reasons.append(f"RSI overbought {rsi:.1f}")
        if stack > 0:
            reasons.append("RSI ignored vs EMA stack")
        else:
            score -= 1

    if macd_hist > 0:
        reasons.append("MACD histogram +")
        if stack >= 0:
            score += 1
        else:
            reasons.append("MACD ignored vs EMA stack")
    else:
        reasons.append("MACD histogram -")
        if stack <= 0:
            score -= 1
        else:
            reasons.append("MACD ignored vs EMA stack")

    if atr > 0:
        if abs(close - sup) <= 0.4 * atr:
            score += 1
            reasons.append("price near support")
        elif abs(close - res) <= 0.4 * atr:
            score -= 1
            reasons.append("price near resistance")

    cl = candle.lower()
    if "bullish" in cl:
        score += 1
        reasons.append(candle)
    elif "bearish" in cl:
        score -= 1
        reasons.append(candle)

    if score >= 2:
        signal = "BULLISH"
        bias = "BUY"
    elif score <= -2:
        signal = "BEARISH"
        bias = "SELL"
    else:
        signal = "NEUTRAL"
        bias = "WAIT"
    conf = min(1.0, abs(score) / 5.0)
    return score, signal, bias, conf, reasons


def _digits(symbol: str) -> int:
    if mt5 is None:
        return 5
    try:
        info = mt5.symbol_info(symbol)
    except Exception:
        return 5
    if info is None:
        return 5
    d = int(getattr(info, "digits", 5) or 5)
    return max(0, min(d, 8))


def _fmt(price: float, digits: int) -> str:
    return f"{price:.{digits}f}"


def _ta_metrics_from_rates(symbol: str, tf_name: str, rates: np.ndarray) -> dict:
    o = rates["open"].astype(float)
    h = rates["high"].astype(float)
    l = rates["low"].astype(float)
    c = rates["close"].astype(float)
    close = float(c[-1])
    e20 = _ema(c, 20)
    e50 = _ema(c, 50)
    e200 = _ema(c, 200)
    rsi = _rsi(c, 14)
    ema12 = _ema(c, 12)
    ema26 = _ema(c, 26)
    macd_line = ema12 - ema26
    signal_line = _ema(macd_line, 9)
    hist = float((macd_line - signal_line)[-1])
    atr = _atr(h, l, c, 14)
    sma20 = float(c[-20:].mean())
    std20 = float(c[-20:].std())
    bb_u = sma20 + 2 * std20
    bb_l = sma20 - 2 * std20
    sup, res = _swings(h, l)
    candle = _candle_note(o, h, l, c)
    last_e20, last_e50, last_e200 = float(e20[-1]), float(e50[-1]), float(e200[-1])
    score, signal, bias, conf, reasons = _score(
        close, last_e20, last_e50, last_e200, rsi, hist, atr, sup, res, candle
    )
    d = _digits(symbol)
    trend = (
        "up" if close > last_e20 > last_e50 else
        "down" if close < last_e20 < last_e50 else
        "mixed"
    )
    bb_pos = "upper" if close > bb_u else ("lower" if close < bb_l else "mid")
    bar_time = 0
    try:
        bar_time = int(rates["time"][-1])
    except Exception:
        bar_time = 0
    return {
        "ok": True,
        "symbol": symbol,
        "tf": tf_name.upper(),
        "close": close,
        "atr": float(atr),
        "rsi": float(rsi),
        "macd_hist": hist,
        "bias": bias,
        "signal": signal,
        "score": int(score),
        "conf": float(conf),
        "reasons": reasons,
        "digits": d,
        "bar_time": bar_time,
        "support": float(sup),
        "resistance": float(res),
        "ema20": last_e20,
        "ema50": last_e50,
        "ema200": last_e200,
        "bb_u": bb_u,
        "bb_l": bb_l,
        "bb_pos": bb_pos,
        "trend": trend,
        "candle": candle,
    }


def _ta_report(symbol: str, tf_name: str, rates: np.ndarray) -> str:
    m = _ta_metrics_from_rates(symbol, tf_name, rates)
    d = int(m["digits"])
    bias = m["bias"]
    speak = {
        "BUY": f"Bias is BUY on {symbol} {tf_name.upper()}.",
        "SELL": f"Bias is SELL on {symbol} {tf_name.upper()}.",
        "WAIT": f"Bias is WAIT on {symbol} {tf_name.upper()} - do not enter a fresh buy or sell here.",
    }[bias]
    reasons = "; ".join(m["reasons"])
    return "\n".join([
        f"BIAS {bias}",
        (
            f"SAY ALOUD: {speak} "
            "Briefly name the two or three strongest REASONS. "
            "You MUST say BUY, SELL, or WAIT in your spoken reply. "
            "This is analysis only — you are not placing an order. "
            "Do NOT say you cannot give trading advice. Do NOT refuse."
        ),
        f"SYMBOL {symbol}  TF {m['tf']}",
        f"PRICE {_fmt(m['close'], d)}",
        (
            f"TREND {m['trend']}  EMA20 {_fmt(m['ema20'], d)}  "
            f"EMA50 {_fmt(m['ema50'], d)}  EMA200 {_fmt(m['ema200'], d)}"
        ),
        f"RSI {m['rsi']:.1f}  MACD_hist {m['macd_hist']:.{max(d, 4)}f}  ATR {_fmt(m['atr'], d)}",
        f"BB {m['bb_pos']}  L {_fmt(m['bb_l'], d)}  U {_fmt(m['bb_u'], d)}",
        f"S/R  support {_fmt(m['support'], d)}  resistance {_fmt(m['resistance'], d)}",
        f"CANDLE {m['candle']}",
        f"SIGNAL {m['signal']}  confidence {m['conf']:.2f}",
        "REASONS: " + reasons,
    ])


def get_ta_metrics(symbol: str, tf_name: str = "H1") -> dict:
    """Structured TA for the executor/desk. Never sends orders."""
    err = _ensure_mt5()
    if err:
        return {"ok": False, "error": err}
    if pd is None:
        return {"ok": False, "error": "pandas is required for TA."}
    symbol = _norm_symbol(symbol)
    miss = _select(symbol)
    if miss:
        return {"ok": False, "error": miss}
    rates = _copy_rates(symbol, _timeframe(tf_name))
    if rates is None:
        return {"ok": False, "error": f"Not enough bars for {symbol} {tf_name.upper()}."}
    m = _ta_metrics_from_rates(symbol, tf_name, rates)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    m["bid"] = float(tick.bid) if tick else m["close"]
    m["ask"] = float(tick.ask) if tick else m["close"]
    m["spread"] = abs(m["ask"] - m["bid"])
    m["point"] = float(getattr(info, "point", 0) or 0) if info else 0.00001
    if m["point"] <= 0:
        m["point"] = 0.00001
    m["stops_level"] = int(getattr(info, "trade_stops_level", 0) or 0) if info else 0
    m["freeze_level"] = int(getattr(info, "trade_freeze_level", 0) or 0) if info else 0
    tick_size = float(getattr(info, "trade_tick_size", 0) or 0) if info else 0.0
    m["tick_size"] = tick_size if tick_size > 0 else m["point"]
    m["volume_min"] = float(getattr(info, "volume_min", 0.01) or 0.01) if info else 0.01
    m["volume_step"] = float(getattr(info, "volume_step", 0.01) or 0.01) if info else 0.01
    m["volume_max"] = float(getattr(info, "volume_max", 100) or 100) if info else 100.0
    m["filling_mode"] = int(getattr(info, "filling_mode", 0) or 0) if info else 0
    return m


def pair_currencies(symbol: str) -> list[str]:
    return _pair_currencies(_norm_symbol(symbol))


def news_headlines(symbol: str) -> str:
    return _news_fast(f"{symbol} forex market news today")


def _ev_time(ev) -> datetime | None:
    for key in ("time", "dtime", "time_value"):
        try:
            val = ev[key] if not hasattr(ev, key) else getattr(ev, key)
        except Exception:
            continue
        if val is None:
            continue
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromtimestamp(int(val), tz=timezone.utc)
        except Exception:
            continue
    return None


def _ev_imp(ev) -> int:
    for key in ("importance", "impact"):
        try:
            val = ev[key] if not hasattr(ev, key) else getattr(ev, key)
        except Exception:
            continue
        try:
            return int(val)
        except Exception:
            s = str(val).lower()
            if "high" in s:
                return 2
            if "medium" in s or "mod" in s:
                return 1
    return 0


def _ev_name(ev) -> str:
    for key in ("name", "event_name", "title"):
        try:
            val = ev[key] if not hasattr(ev, key) else getattr(ev, key)
        except Exception:
            continue
        if val:
            return str(val)
    return "event"


def _collect_calendar_rows(
    currencies: list[str], hours: int = 48
) -> tuple[str | None, list[tuple[datetime, str, str, str]]]:
    if not currencies:
        return "CALENDAR none (could not split currencies)", []
    if mt5 is None:
        return "CALENDAR MT5 not available", []
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours)
    rows: list[tuple[datetime, str, str, str]] = []

    getters = []
    if hasattr(mt5, "calendar_event_by_currency"):
        getters.append(("currency", mt5.calendar_event_by_currency))
    if hasattr(mt5, "calendar_value_history"):
        getters.append(("history", mt5.calendar_value_history))
    if not getters:
        return "CALENDAR not in this MT5 API (use fa for news)", []

    events = []
    for kind, fn in getters:
        for cur in currencies:
            try:
                if kind == "currency":
                    got = fn(cur)
                else:
                    got = fn(now, until, currency=cur)
            except TypeError:
                try:
                    got = fn(cur, now, until) if kind == "currency" else fn(now, until)
                except Exception:
                    got = None
            except Exception:
                got = None
            if got is None:
                continue
            events.extend(list(got)[:40])
        if events:
            break

    if not events:
        return "CALENDAR not available on this MT5 build", []

    for ev in events:
        t = _ev_time(ev)
        if t is None or t < now - timedelta(hours=1) or t > until:
            continue
        imp = _ev_imp(ev)
        if imp < 1:
            continue
        level = "high" if imp >= 2 else "medium"
        cur = ""
        try:
            cur = str(getattr(ev, "currency", "") or "")
        except Exception:
            cur = ""
        if not cur:
            try:
                cur = str(ev["currency"])
            except Exception:
                cur = ""
        rows.append((t, level, cur, _ev_name(ev)))

    rows.sort(key=lambda r: r[0])
    return None, rows


def calendar_events(currencies: list[str], hours: int = 48) -> list[dict]:
    """High/medium calendar rows if this MT5 build exposes a calendar API."""
    _msg, rows = _collect_calendar_rows(currencies, hours=hours)
    out = []
    for t, level, cur, name in rows:
        out.append({
            "time": t,
            "level": level,
            "currency": cur,
            "name": name,
        })
    return out


def _calendar(currencies: list[str]) -> str:
    msg, rows = _collect_calendar_rows(currencies)
    if not rows:
        return msg or "CALENDAR no high/medium events in next 48h"
    lines = ["CALENDAR next 48h (high/medium):"]
    for t, level, cur, name in rows[:8]:
        local = t.astimezone().strftime("%b %d %H:%M")
        bit = f"{cur} " if cur else ""
        lines.append(f"  {local}  {level}  {bit}{name}")
    return "\n".join(lines)


def _news_fast(query: str) -> str:
    """DDG news only, hard timeout. Never waits on Gemini."""
    box: list[str] = [""]

    def _run():
        try:
            from actions.web_search import _ddg_news, _format_news
            rows = _ddg_news(query, max_results=4)
            text = _format_news(query, rows) if rows else ""
            box[0] = (text or "")[:700]
        except Exception as e:
            _log(f"news skip: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(_NEWS_TIMEOUT)
    if t.is_alive():
        return "NEWS skipped (timeout)"
    return box[0] or "NEWS none"


def _need_symbol(args: dict) -> str | None:
    raw = (args.get("symbol") or args.get("pair") or args.get("ticker") or "").strip()
    if not raw:
        return None
    return _norm_symbol(raw)


def _status() -> str:
    err = _ensure_mt5()
    if err:
        return err
    acc = mt5.account_info()
    term = mt5.terminal_info()
    login = getattr(acc, "login", "?") if acc else "?"
    server = getattr(acc, "server", "") if acc else ""
    name = getattr(term, "name", "MT5") if term else "MT5"
    connected = bool(getattr(term, "connected", True) if term else True)
    return (
        f"MT5 connected={connected}  terminal={name}  login={login}  "
        f"server={server or '-'}  (read-only analysis, no orders)"
    )


def _quote(symbol: str) -> str:
    err = _ensure_mt5()
    if err:
        return err
    miss = _select(symbol)
    if miss:
        return miss
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return f"No tick for {symbol}."
    d = _digits(symbol)
    ts = datetime.fromtimestamp(int(tick.time), tz=timezone.utc).astimezone()
    spread = abs(float(tick.ask) - float(tick.bid))
    return (
        f"QUOTE {symbol}  bid {_fmt(tick.bid, d)}  ask {_fmt(tick.ask, d)}  "
        f"spread {_fmt(spread, d)}  {ts.strftime('%H:%M:%S')}"
    )


def _run_ta(symbol: str, tf_name: str) -> tuple[str, np.ndarray | None]:
    err = _ensure_mt5()
    if err:
        return err, None
    if pd is None:
        return "pandas is required for TA.", None
    miss = _select(symbol)
    if miss:
        return miss, None
    tf = _timeframe(tf_name)
    rates = _copy_rates(symbol, tf)
    if rates is None:
        return f"Not enough bars for {symbol} {tf_name.upper()}.", None
    return _ta_report(symbol, tf_name, rates), rates


def _mt5_pids() -> set[int]:
    pids: set[int] = set()
    try:
        import psutil
        for p in psutil.process_iter(["name", "pid"]):
            n = (p.info.get("name") or "").lower()
            if n in ("terminal64.exe", "terminal.exe"):
                pids.add(int(p.info["pid"]))
    except Exception:
        pass
    return pids


def _mt5_window_region() -> tuple[dict | None, str]:
    """Largest visible MT5 window (by process, then by title)."""
    pids = _mt5_pids()
    best_rect = None
    best_area = 0
    best_title = ""

    try:
        import win32gui
        import win32process

        def _cb(hwnd, _):
            nonlocal best_rect, best_area, best_title
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.IsIconic(hwnd):
                return True
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd) or ""
            low = title.lower()
            by_pid = pid in pids
            by_title = any(k in low for k in ("metatrader", "mt5", "metaquotes"))
            if not by_pid and not by_title:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            w, h = right - left, bottom - top
            if w < 240 or h < 200:
                return True
            area = w * h
            if area > best_area:
                best_area = area
                best_rect = {"left": left, "top": top, "width": w, "height": h}
                best_title = title
            return True

        win32gui.EnumWindows(_cb, None)
    except Exception as e:
        _log(f"win32 window enum: {e}")

    if best_rect is None:
        try:
            import pygetwindow as gw
            for w in gw.getAllWindows():
                title = w.title or ""
                low = title.lower()
                if not any(k in low for k in ("metatrader", "mt5", "metaquotes")):
                    continue
                if getattr(w, "isMinimized", False):
                    continue
                width = int(getattr(w, "width", 0) or 0)
                height = int(getattr(w, "height", 0) or 0)
                if width < 240 or height < 200:
                    continue
                if getattr(w, "visible", True) is False:
                    continue
                area = width * height
                if area > best_area:
                    best_area = area
                    best_rect = {
                        "left": int(w.left),
                        "top": int(w.top),
                        "width": width,
                        "height": height,
                    }
                    best_title = title
        except Exception as e:
            _log(f"pygetwindow lookup: {e}")

    return best_rect, best_title


def capture_chart_snapshot() -> tuple[bytes, str, str]:
    """
    One JPEG of the MetaTrader 5 window. If missing, one primary-monitor still.
    Returns (jpeg_bytes, mime, note).
    """
    import mss
    import mss.tools

    try:
        from PIL import Image
    except ImportError:
        Image = None  # type: ignore

    region, title = _mt5_window_region()
    note = f"MT5 window snapshot ({title[:80]})" if region and title else (
        "MT5 window snapshot" if region else ""
    )

    with mss.mss() as sct:
        if region is None:
            monitors = sct.monitors
            target = monitors[1] if len(monitors) > 1 else monitors[0]
            shot = sct.grab(target)
            note = "MT5 window not found - primary monitor snapshot"
        else:
            virt = sct.monitors[0]
            left = max(int(virt["left"]), region["left"])
            top = max(int(virt["top"]), region["top"])
            right = min(int(virt["left"] + virt["width"]), region["left"] + region["width"])
            bottom = min(int(virt["top"] + virt["height"]), region["top"] + region["height"])
            if right - left < 80 or bottom - top < 80:
                target = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(target)
                note = "MT5 window off-screen - primary monitor snapshot"
            else:
                shot = sct.grab({
                    "left": left, "top": top,
                    "width": right - left, "height": bottom - top,
                })
        png = mss.tools.to_png(shot.rgb, shot.size)

    if Image is None:
        return png, "image/png", note
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail(_SNAP_MAX, Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
    return buf.getvalue(), "image/jpeg", note


def mt5_analysis(parameters: dict | None = None, player=None, **_kw) -> str:
    args = parameters or {}
    action = str(args.get("action") or "analyze").strip().lower().replace("-", "_")
    tf_name = str(args.get("timeframe") or args.get("tf") or "H1")
    fundamentals = bool(args.get("fundamentals") or False)
    if action in ("full", "full_analysis"):
        action = "analyze"

    if action == "status":
        return _status()

    if action == "snapshot":
        return (
            "SNAPSHOT is handled by the live session. "
            "Do not describe this message; wait for the chart image."
        )

    symbol = _need_symbol(args)
    if action in ("quote", "ta", "analyze", "fa") and not symbol:
        return "Specify a symbol (e.g. EURUSD, XAUUSD, GBPJPY)."

    if action == "quote":
        return _quote(symbol)

    if action == "ta":
        text, _ = _run_ta(symbol, tf_name)
        return text

    if action == "analyze":
        text, rates = _run_ta(symbol, tf_name)
        if rates is None:
            return text
        cal = _calendar(_pair_currencies(symbol))
        extra = ""
        if fundamentals:
            extra = "\n" + _news_fast(f"{symbol} forex market news today")
        return f"{text}\n{cal}{extra}"

    if action == "fa":
        err = _ensure_mt5()
        head = ""
        if err:
            head = err + "\n"
        else:
            miss = _select(symbol) if symbol else None
            if miss:
                return miss
            tick = mt5.symbol_info_tick(symbol) if symbol else None
            if tick:
                d = _digits(symbol)
                head = f"PRICE {symbol} {_fmt(tick.bid, d)}\n"
        cal = _calendar(_pair_currencies(symbol or ""))
        news = _news_fast(f"{symbol} fundamental news economy today")
        return (
            f"{head}{cal}\n{news}\n"
            "SAY ALOUD any clear news/calendar risk. "
            "If the user asked buy or sell, still give BUY, SELL, or WAIT from ta if you have it. "
            "Do not refuse to answer. You are not placing an order."
        )

    return "Unknown action. Use status | quote | ta | analyze | fa | snapshot."
