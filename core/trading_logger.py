"""
Dedicated trading-desk logging. Isolated from the assistant's logs/athena.log.

Writes:
  logs/trading/desk.log          — rotating human-readable log
  logs/trading/decisions.jsonl   — one JSON object per desk/watch/exec decision
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_DIR = _base_dir() / "logs" / "trading"
_LOG_FILE = _DIR / "desk.log"
_JSONL_FILE = _DIR / "decisions.jsonl"
_logger: logging.Logger | None = None
_hud_sink: Callable[[str], None] | None = None
_lock = threading.Lock()


def log_dir() -> Path:
    return _DIR


def log_path() -> Path:
    return _LOG_FILE


def decisions_path() -> Path:
    return _JSONL_FILE


def set_hud_sink(callback: Callable[[str], None] | None) -> None:
    global _hud_sink
    _hud_sink = callback


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("athena.trading.desk")
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
        backupCount=8,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    try:
        if hasattr(sh.stream, "reconfigure"):
            sh.stream.reconfigure(errors="replace")
    except Exception:
        pass
    logger.addHandler(sh)

    _logger = logger
    logger.info("Trading desk log started -> %s", _LOG_FILE)
    logger.info("Decisions JSONL -> %s", _JSONL_FILE)
    return logger


def tlog(msg: str, level: str = "info", *, hud: bool = False) -> None:
    logger = get_logger()
    lvl = (level or "info").lower()
    if lvl == "debug":
        logger.debug(msg)
    elif lvl == "warning":
        logger.warning(msg)
    elif lvl == "error":
        logger.error(msg)
    else:
        logger.info(msg)
    if hud and _hud_sink:
        try:
            prefix = "" if msg.startswith(("SYS:", "ERR:", "You:", "[", "DESK")) else "SYS: "
            _hud_sink(f"{prefix}{msg}")
        except Exception:
            pass


def decision(event: str, **fields: Any) -> None:
    """Append one structured decision. Also writes a one-line summary to desk.log."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **{k: v for k, v in fields.items() if v is not None},
    }
    line = json.dumps(rec, default=str, ensure_ascii=False)
    with _lock:
        try:
            _DIR.mkdir(parents=True, exist_ok=True)
            with _JSONL_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            tlog(f"jsonl write failed: {e}", "error")
    bits = [event]
    for key in ("status", "symbol", "bias", "score", "reason", "ticket", "side"):
        if key in rec:
            bits.append(f"{key}={rec[key]}")
    tlog(" | ".join(str(b) for b in bits))
