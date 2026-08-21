"""Human-readable trading activity log (non-technical)."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from security.sanitize import sanitize


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_DIR = _base_dir() / "logs" / "trading"
_FILE = _DIR / "activity.log"
_logger: logging.Logger | None = None
_hud: Callable[[str], None] | None = None


def set_hud_sink(cb: Callable[[str], None] | None) -> None:
    global _hud
    _hud = cb


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    _DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("athena.trading.activity")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    fh = RotatingFileHandler(_FILE, maxBytes=2_000_000, backupCount=8, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)
    _logger = logger
    return logger


def activity(message: str, *, hud: bool = True) -> str:
    """Record one user-facing line. Returns the stamped sentence."""
    msg = sanitize((message or "").strip())
    now = datetime.now().strftime("%H:%M")
    line = f"{now} — {msg}" if not msg.startswith(tuple("0123456789")) else msg
    get_logger().info(line)
    if hud and _hud:
        try:
            _hud(f"SYS: {line}")
        except Exception:
            pass
    return line
