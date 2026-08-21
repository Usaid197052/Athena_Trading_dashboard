"""
Demo-only MT5 executor. Places/closes Athena magic-number tickets with SL/TP.
Never imported by main.py. Gemini never calls this module.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # type: ignore

from actions.mt5_analysis import _ensure_mt5, _norm_symbol, _select, recover_mt5, ensure_algo_trading


MAGIC_DEFAULT = 20260820


def _tlog(msg: str, level: str = "info") -> None:
    print(f"[MT5-X] {msg}")
    try:
        from core.trading_logger import tlog
        tlog(f"executor: {msg}", level)
    except Exception:
        pass


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def is_demo_account() -> tuple[bool, str]:
    err = _ensure_mt5()
    if err:
        return False, err
    acc = mt5.account_info()
    if acc is None:
        return False, "No MT5 account info. Open the terminal and log in."
    mode = int(getattr(acc, "trade_mode", -1))
    demo = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
    contest = getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1)
    if mode not in (demo, contest):
        return False, (
            f"BLOCKED live: account {acc.login} on {acc.server} is not a demo. "
            "Athena will not send orders on a real account."
        )
    return True, ""


def account_snapshot() -> dict[str, Any]:
    err = _ensure_mt5()
    if err:
        return {"ok": False, "error": err}
    acc = mt5.account_info()
    if acc is None:
        return {"ok": False, "error": "No MT5 account info."}
    demo, reason = is_demo_account()
    return {
        "ok": True,
        "demo": demo,
        "reason": reason,
        "login": int(getattr(acc, "login", 0) or 0),
        "server": str(getattr(acc, "server", "") or ""),
        "name": str(getattr(acc, "name", "") or ""),
        "balance": float(getattr(acc, "balance", 0) or 0),
        "equity": float(getattr(acc, "equity", 0) or 0),
        "margin": float(getattr(acc, "margin", 0) or 0),
        "profit": float(getattr(acc, "profit", 0) or 0),
        "currency": str(getattr(acc, "currency", "") or ""),
        "leverage": int(getattr(acc, "leverage", 0) or 0),
        "trade_mode": int(getattr(acc, "trade_mode", -1)),
    }


def _positions(magic: int, symbol: str | None = None) -> list:
    err = _ensure_mt5()
    if err:
        return []
    if symbol:
        symbol = _norm_symbol(symbol)
        got = mt5.positions_get(symbol=symbol)
    else:
        got = mt5.positions_get()
    if got is None:
        return []
    return [p for p in got if int(getattr(p, "magic", 0) or 0) == int(magic)]


def magic_positions(magic: int, symbol: str | None = None) -> list[dict]:
    rows = []
    for p in _positions(magic, symbol):
        side = "BUY" if int(p.type) == mt5.POSITION_TYPE_BUY else "SELL"
        rows.append({
            "ticket": int(p.ticket),
            "symbol": str(p.symbol),
            "side": side,
            "volume": float(p.volume),
            "open": float(p.price_open),
            "sl": float(p.sl),
            "tp": float(p.tp),
            "profit": float(p.profit) + float(getattr(p, "swap", 0) or 0),
            "magic": int(p.magic),
        })
    return rows


def daily_pnl(magic: int) -> float:
    """Realized deals today (UTC) plus floating P&L on open Athena tickets."""
    err = _ensure_mt5()
    if err:
        return 0.0
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    realized = 0.0
    try:
        deals = mt5.history_deals_get(start, now)
    except Exception:
        deals = None
    if deals:
        for d in deals:
            if int(getattr(d, "magic", 0) or 0) != int(magic):
                continue
            realized += float(getattr(d, "profit", 0) or 0)
            realized += float(getattr(d, "swap", 0) or 0)
            realized += float(getattr(d, "commission", 0) or 0)
    floating = sum(p["profit"] for p in magic_positions(magic))
    return realized + floating


def _filling(info) -> int:
    fm = int(getattr(info, "filling_mode", 0) or 0)
    fok = getattr(mt5, "ORDER_FILLING_FOK", 0)
    ioc = getattr(mt5, "ORDER_FILLING_IOC", 1)
    ret = getattr(mt5, "ORDER_FILLING_RETURN", 2)
    if fm & 1:
        return fok
    if fm & 2:
        return ioc
    return ret


def _norm_volume(info, volume: float) -> float:
    vmin = float(getattr(info, "volume_min", 0.01) or 0.01)
    vmax = float(getattr(info, "volume_max", 100) or 100)
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    if step <= 0:
        step = 0.01
    vol = max(vmin, min(vmax, float(volume)))
    steps = round(vol / step)
    vol = steps * step
    vol = max(vmin, min(vmax, vol))
    return float(f"{vol:.2f}") if step >= 0.01 else vol


def _norm_price(price: float, digits: int, tick_size: float) -> float:
    if tick_size and tick_size > 0:
        price = round(price / tick_size) * tick_size
    return round(price, digits)


def _retcode_text(code: int) -> str:
    names = {
        10004: "requote",
        10006: "rejected",
        10007: "cancel",
        10008: "placed",
        10009: "done",
        10010: "done_partial",
        10011: "error",
        10012: "timeout",
        10013: "invalid",
        10014: "invalid_volume",
        10015: "invalid_price",
        10016: "invalid_stops",
        10017: "trade_disabled",
        10018: "market_closed",
        10019: "no_money",
        10020: "price_changed",
        10021: "off_quotes",
        10027: "autotrading_disabled",
        10030: "invalid_filling",
    }
    return names.get(int(code), str(code))


def place_market(
    *,
    symbol: str,
    side: str,
    volume: float,
    sl: float,
    tp: float,
    magic: int = MAGIC_DEFAULT,
    deviation: int = 20,
    comment: str = "Athena",
) -> dict[str, Any]:
    """Market order with mandatory SL/TP. Demo only. One Athena position per symbol."""
    demo, reason = is_demo_account()
    if not demo:
        return {"ok": False, "status": "BLOCKED", "reason": reason or "not demo"}

    at_ok, at_why = ensure_algo_trading()
    if not at_ok:
        return {"ok": False, "status": "BLOCKED", "reason": at_why}

    symbol = _norm_symbol(symbol)
    miss = _select(symbol)
    if miss:
        return {"ok": False, "status": "BLOCKED", "reason": miss}

    side_u = (side or "").strip().upper()
    if side_u not in ("BUY", "SELL"):
        return {"ok": False, "status": "BLOCKED", "reason": f"Invalid side {side}"}
    if sl is None or tp is None or float(sl) <= 0 or float(tp) <= 0:
        return {"ok": False, "status": "BLOCKED", "reason": "SL and TP are required"}

    existing = magic_positions(magic, symbol)
    if existing:
        have = existing[0]
        if have["side"] == side_u:
            return {
                "ok": False,
                "status": "BLOCKED",
                "reason": f"Already in {side_u} {symbol} ticket {have['ticket']}",
                "ticket": have["ticket"],
            }
        closed = close_magic(magic=magic, symbol=symbol)
        return {
            "ok": bool(closed.get("ok")),
            "status": "CLOSED",
            "reason": (
                f"Opposite BIAS {side_u} vs open {have['side']} — closed only, no reverse."
            ),
            "close": closed,
        }

    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        rec = recover_mt5(f"place_market no tick/info {symbol}")
        if rec is None:
            info = mt5.symbol_info(symbol)
            tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return {"ok": False, "status": "BLOCKED", "reason": f"No tick/info for {symbol}"}

    vol = _norm_volume(info, volume)
    digits = int(getattr(info, "digits", 5) or 5)
    tick_size = float(getattr(info, "trade_tick_size", 0) or 0) or float(info.point)
    order_type = mt5.ORDER_TYPE_BUY if side_u == "BUY" else mt5.ORDER_TYPE_SELL
    price = float(tick.ask) if side_u == "BUY" else float(tick.bid)
    sl_n = _norm_price(float(sl), digits, tick_size)
    tp_n = _norm_price(float(tp), digits, tick_size)
    if side_u == "BUY" and not (sl_n < price < tp_n):
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": f"BUY stops invalid sl={sl_n} price={price} tp={tp_n}",
        }
    if side_u == "SELL" and not (tp_n < price < sl_n):
        return {
            "ok": False,
            "status": "BLOCKED",
            "reason": f"SELL stops invalid tp={tp_n} price={price} sl={sl_n}",
        }

    fillings = [_filling(info)]
    for alt in (
        getattr(mt5, "ORDER_FILLING_IOC", 1),
        getattr(mt5, "ORDER_FILLING_FOK", 0),
        getattr(mt5, "ORDER_FILLING_RETURN", 2),
    ):
        if alt not in fillings:
            fillings.append(alt)

    last_reason = "order_send failed"
    for filling in fillings:
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": vol,
            "type": order_type,
            "price": price,
            "sl": sl_n,
            "tp": tp_n,
            "deviation": int(deviation),
            "magic": int(magic),
            "comment": (comment or "Athena")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        check = mt5.order_check(request)
        if check is None:
            last_reason = f"order_check failed {mt5.last_error()}"
            rec = recover_mt5(f"order_check None {symbol} {last_reason}")
            if rec is None:
                check = mt5.order_check(request)
            if check is None:
                continue
        if int(getattr(check, "retcode", 1)) not in (0, 10009):
            rc = int(check.retcode)
            last_reason = (
                f"order_check {_retcode_text(rc)} ({rc}) "
                f"{getattr(check, 'comment', '') or ''}"
            ).strip()
            if rc == 10030:
                continue
            return {"ok": False, "status": "BLOCKED", "reason": last_reason}

        result = mt5.order_send(request)
        if result is None:
            last_reason = f"order_send failed {mt5.last_error()}"
            rec = recover_mt5(f"order_send None {symbol} {last_reason}")
            if rec is None:
                result = mt5.order_send(request)
            if result is None:
                continue
        rc = int(result.retcode)
        if rc not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL, 10009, 10010):
            last_reason = (
                f"order_send {_retcode_text(rc)} ({rc}) "
                f"{getattr(result, 'comment', '')}"
            )
            if rc == 10030:
                continue
            return {"ok": False, "status": "BLOCKED", "reason": last_reason}

        fill = float(getattr(result, "price", 0) or price)
        ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
        _tlog(f"FILLED {side_u} {symbol} vol={vol} @ {fill} sl={sl_n} tp={tp_n} ticket={ticket}")
        return {
            "ok": True,
            "status": "FILLED",
            "side": side_u,
            "symbol": symbol,
            "volume": vol,
            "price": fill,
            "sl": sl_n,
            "tp": tp_n,
            "ticket": ticket,
            "deal": int(getattr(result, "deal", 0) or 0),
            "comment": str(getattr(result, "comment", "") or ""),
        }
    return {"ok": False, "status": "BLOCKED", "reason": last_reason}


def close_magic(*, magic: int, symbol: str | None = None) -> dict[str, Any]:
    """Close Athena positions only (magic filter). Demo check still applies."""
    demo, reason = is_demo_account()
    if not demo:
        return {"ok": False, "status": "BLOCKED", "reason": reason or "not demo"}

    positions = _positions(magic, symbol)
    if not positions:
        return {"ok": True, "status": "CLOSED", "closed": 0, "reason": "No Athena positions"}

    closed = []
    errors = []
    for pos in positions:
        tick = mt5.symbol_info_tick(pos.symbol)
        info = mt5.symbol_info(pos.symbol)
        if tick is None or info is None:
            errors.append(f"{pos.symbol} no tick")
            continue
        if int(pos.type) == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)
        fillings = [_filling(info)]
        for alt in (
            getattr(mt5, "ORDER_FILLING_IOC", 1),
            getattr(mt5, "ORDER_FILLING_FOK", 0),
            getattr(mt5, "ORDER_FILLING_RETURN", 2),
        ):
            if alt not in fillings:
                fillings.append(alt)
        sent = False
        last_err = ""
        for filling in fillings:
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": int(pos.ticket),
                "symbol": str(pos.symbol),
                "volume": float(pos.volume),
                "type": order_type,
                "price": price,
                "deviation": 30,
                "magic": int(magic),
                "comment": "Athena flatten",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            result = mt5.order_send(request)
            if result is None:
                last_err = str(mt5.last_error())
                continue
            rc = int(result.retcode)
            if rc in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL, 10009, 10010):
                closed.append(int(pos.ticket))
                _tlog(f"CLOSED {pos.symbol} ticket={pos.ticket}")
                sent = True
                break
            last_err = f"{_retcode_text(rc)} {getattr(result, 'comment', '')}"
            if rc != 10030:
                break
        if not sent:
            errors.append(f"{pos.symbol} #{pos.ticket} {last_err}")

    ok = not errors
    return {
        "ok": ok,
        "status": "CLOSED" if closed else "BLOCKED",
        "closed": len(closed),
        "tickets": closed,
        "errors": errors,
        "reason": "; ".join(errors) if errors else f"Closed {len(closed)} ticket(s)",
    }
