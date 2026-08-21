"""Developer/debug log for agent lifecycle. Never writes secrets."""
from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from security.sanitize import sanitize, sanitize_obj


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_DIR = _base_dir() / "logs" / "trading"
_LOG = _DIR / "agents.debug.log"
_JSONL = _DIR / "agents.jsonl"
_logger: logging.Logger | None = None
_lock = threading.Lock()


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    _DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("athena.trading.agents")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(_LOG, maxBytes=4_000_000, backupCount=8, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    _logger = logger
    logger.info("Agent debug log started -> %s", _LOG)
    return logger


def debug(msg: str, level: str = "info") -> None:
    logger = get_logger()
    text = sanitize(msg)
    lvl = (level or "info").lower()
    if lvl == "debug":
        logger.debug(text)
    elif lvl == "warning":
        logger.warning(text)
    elif lvl == "error":
        logger.error(text)
    else:
        logger.info(text)


def event(kind: str, **fields: Any) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": kind,
        **{k: v for k, v in fields.items() if v is not None},
    }
    rec = sanitize_obj(rec)
    line = json.dumps(rec, default=str, ensure_ascii=False)
    debug(f"{kind} | " + " ".join(
        f"{k}={rec[k]}" for k in ("agent", "model", "task_id", "status", "elapsed_ms")
        if k in rec
    ))
    with _lock:
        try:
            _DIR.mkdir(parents=True, exist_ok=True)
            with _JSONL.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            debug(f"agents.jsonl write failed: {e}", "error")
