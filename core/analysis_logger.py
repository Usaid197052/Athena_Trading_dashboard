"""
Isolated analysis + trade log. Not mixed into desk.log.

Writes:
  logs/trading/analysis.log    — rotating human report per desk run
  logs/trading/analysis.jsonl  — full JSON snapshot per desk run
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_DIR = _base_dir() / "logs" / "trading"
_LOG_FILE = _DIR / "analysis.log"
_JSONL_FILE = _DIR / "analysis.jsonl"
_logger: logging.Logger | None = None
_lock = threading.Lock()


def log_dir() -> Path:
    return _DIR


def log_path() -> Path:
    return _LOG_FILE


def jsonl_path() -> Path:
    return _JSONL_FILE


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("athena.trading.analysis")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=4_000_000,
        backupCount=12,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    _logger = logger
    logger.info("Analysis log started -> %s", _LOG_FILE)
    logger.info("Analysis JSONL -> %s", _JSONL_FILE)
    return logger


def _fmt_num(val: Any, digits: int = 5) -> str:
    try:
        return f"{float(val):.{max(0, int(digits))}f}"
    except (TypeError, ValueError):
        return "-"


def format_report(rec: dict[str, Any]) -> str:
    ta = rec.get("ta") or {}
    plan = rec.get("plan") or {}
    gates = rec.get("gates") or {}
    exe = rec.get("exec")
    digits = int(ta.get("digits") or rec.get("digits") or 5)
    reasons = ta.get("reasons") or rec.get("reasons") or []
    if isinstance(reasons, list):
        why = "; ".join(str(r) for r in reasons if r)
    else:
        why = str(reasons or "")

    ts = str(rec.get("ts") or "")
    symbol = rec.get("symbol") or "?"
    tf = rec.get("tf") or ""
    status = rec.get("status") or "-"
    bias = rec.get("bias") or "-"
    score = rec.get("score")

    lines = [
        f"======== {ts}  {symbol} {tf} ========",
        f"STATUS {status}   BIAS {bias}   SCORE {score}",
        f"WHY    {why or '-'}",
    ]

    if plan and plan.get("side"):
        entry = plan.get("entry")
        sl = plan.get("sl")
        tp = plan.get("tp")
        lines.append(
            f"PLAN   {plan.get('side')} {plan.get('volume')}  "
            f"entry~{_fmt_num(entry, digits)}  "
            f"SL {_fmt_num(sl, digits)}  TP {_fmt_num(tp, digits)}  "
            f"({plan.get('sl_atr')} / {plan.get('tp_atr')} ATR)"
        )
    else:
        lines.append("PLAN   none")

    sess = gates.get("session") or "-"
    news = gates.get("news") or "-"
    risk = gates.get("risk") or "-"
    at = gates.get("autotrading")
    at_s = "ON" if at is True else ("OFF" if at is False else str(at or "-"))
    lines.append(
        f"GATES  session={sess}  news={news}  risk={risk}  AutoTrading={at_s}"
    )

    if isinstance(exe, dict) and exe:
        st = str(exe.get("status") or "")
        if st.upper() == "FILLED":
            lines.append(
                f"FILL   ticket {exe.get('ticket')} @ {exe.get('price')}  "
                f"SL {exe.get('sl')}  TP {exe.get('tp')}"
            )
        else:
            lines.append(
                f"EXEC   {st or '-'}  {exe.get('reason') or ''}  "
                f"ticket={exe.get('ticket', '-')}"
            )
    elif status == "FILLED" and rec.get("ticket"):
        lines.append(f"FILL   ticket {rec.get('ticket')}")
    return "\n".join(lines)


def log_analysis(snapshot: dict[str, Any]) -> None:
    """Write one full desk-run snapshot to analysis.jsonl and analysis.log."""
    rec = dict(snapshot or {})
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    logger = get_logger()
    line = json.dumps(rec, default=str, ensure_ascii=False)
    with _lock:
        try:
            _DIR.mkdir(parents=True, exist_ok=True)
            with _JSONL_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.error("analysis.jsonl write failed: %s", e)
    try:
        logger.info("\n%s", format_report(rec))
    except Exception as e:
        logger.error("analysis.log write failed: %s", e)
    try:
        from core.trading_logger import tlog
        bits = [
            "ANALYSIS",
            rec.get("symbol"),
            rec.get("status"),
            f"bias={rec.get('bias')}",
            f"score={rec.get('score')}",
        ]
        ticket = (rec.get("exec") or {}).get("ticket") if isinstance(rec.get("exec"), dict) else rec.get("ticket")
        if ticket:
            bits.append(f"ticket={ticket}")
        tlog(" ".join(str(b) for b in bits if b is not None))
    except Exception:
        pass
