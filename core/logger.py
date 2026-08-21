"""
Athena file logging — rotating log under logs/athena.log.
Also mirrors lines to the HUD when a write_log callback is set.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_LOG_DIR = _base_dir() / "logs"
_LOG_FILE = _LOG_DIR / "athena.log"
_logger: logging.Logger | None = None
_hud_sink: Callable[[str], None] | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("athena")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    try:
        # Windows consoles often can't print unicode arrows
        if hasattr(sh.stream, "reconfigure"):
            sh.stream.reconfigure(errors="replace")
    except Exception:
        pass
    logger.addHandler(sh)

    _logger = logger
    logger.info("Logging started -> %s", _LOG_FILE)
    return logger


def set_hud_sink(callback: Callable[[str], None] | None) -> None:
    """Optional: forward log lines to the HUD write_log."""
    global _hud_sink
    _hud_sink = callback


def log(msg: str, level: str = "info", *, hud: bool = False) -> None:
    """Write to file/console; optionally also to HUD."""
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
            _hud_sink(msg if msg.startswith(("SYS:", "ERR:", "You:", "[")) else f"SYS: {msg}")
        except Exception:
            pass


def log_path() -> Path:
    return _LOG_FILE


def get_trading_logger() -> logging.Logger:
    """Back-compat alias — trading process uses core.trading_logger."""
    from core.trading_logger import get_logger as _get
    return _get()


def trading_log(msg: str, level: str = "info", *, hud: bool = False) -> None:
    from core.trading_logger import tlog
    tlog(msg, level, hud=hud)


def trading_log_path() -> Path:
    from core.trading_logger import log_path as _p
    return _p()
