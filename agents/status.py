"""Live agent status for the HUD (ready / busy / disabled / error)."""
from __future__ import annotations

import threading
from typing import Callable

_lock = threading.Lock()
_states: dict[str, str] = {
    "athena": "ready",
    "deepseek": "ready",
    "qwen": "ready",
    "graph": "disabled",
}
_listeners: list[Callable[[dict[str, str]], None]] = []


def snapshot() -> dict[str, str]:
    with _lock:
        return dict(_states)


def subscribe(cb: Callable[[dict[str, str]], None]) -> None:
    with _lock:
        _listeners.append(cb)


def set_status(name: str, status: str) -> None:
    with _lock:
        _states[str(name)] = str(status)
        copy = dict(_states)
        listeners = list(_listeners)
    for cb in listeners:
        try:
            cb(copy)
        except Exception:
            pass


def set_graph_enabled(enabled: bool) -> None:
    set_status("graph", "ready" if enabled else "disabled")
