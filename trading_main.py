"""
Trading-only Athena. Separate process from main.py — no assistant tools.
Gemini Flash is the text orchestrator. Local agents analyse; they never place orders.
Run: python trading_main.py
"""
from __future__ import annotations

import platform as _platform
import subprocess as _subprocess

if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen

import asyncio
import multiprocessing
import os
import re
import sys
import threading
import traceback
from pathlib import Path

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

from ui import AthenaUI
from memory.config_manager import DEFAULT_ASSISTANT_NAME, get_gemini_key
from actions.mt5_analysis import mt5_analysis, start_mt5_keepalive, _ensure_mt5
from actions.trading_desk import (
    load_trading_config,
    refresh_hud,
    set_paused,
    trading_control,
    trading_desk,
    watch_tick,
)
from core.activity_logger import activity, set_hud_sink as set_activity_hud
from core.agent_debug_logger import get_logger as get_agent_debug_logger
from core.trading_logger import (
    get_logger as get_trading_logger,
    log_path as trading_log_path,
    set_hud_sink as set_trading_hud_sink,
    tlog,
)
from core.analysis_logger import get_logger as get_analysis_logger, log_path as analysis_log_path
from core.mt5_log import set_hud_sink as set_mt5_hud_sink, log_path as mt5_log_path
from agents.config import graph_enabled
from agents.status import set_graph_enabled, set_status, snapshot as agent_snapshot, subscribe as subscribe_status


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)


def classify_exit_intent(text: str) -> str | None:
    raw = (text or "").strip().lower()
    if not raw:
        return None
    compact = re.sub(r"[\s'_]+", "", raw)
    cleaned = re.sub(r"\s+", " ", raw)
    if any(x in cleaned for x in (
        "the computer", "the pc", "the laptop", "my computer", "my pc",
        "this computer", "this pc",
    )):
        return None
    if re.search(r"\b(pc|computer|laptop|desktop)\b", cleaned):
        return None
    if any(p in compact for p in (
        "shutdownyourself", "quitathena", "exitathena", "shutdowncompletely",
        "athenashutdown", "shutyourself",
    )):
        return "shutdown"
    if any(p in cleaned for p in (
        "quit athena", "shut down completely", "shut yourself down", "shutdown",
    )):
        return "shutdown"
    if re.search(r"shut\s*down", cleaned) or "shutdown" in compact:
        return "shutdown"
    return None


def _help_text() -> str:
    return (
        "Ask Athena anything, or use: analyze [symbol] [timeframe] · pause · resume · flatten · "
        "status · run desk [symbol] · quote [symbol] · sleep. "
        "Default pair is EURUSD. Analyze runs agents (technical leads); if auto-trade is ON "
        "and agents say BUY/SELL, a demo order is placed automatically — you do not need run desk."
    )


class TradingSession:
    """Text-first trading process: HUD + watch-loop + multi-agent analysis."""

    def __init__(self, ui: AthenaUI):
        self.ui = ui
        self._asst_name = DEFAULT_ASSISTANT_NAME
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_started = False
        self._hud_sleeping = False
        self._sleep_request: asyncio.Event | None = None
        self._wake_event: asyncio.Event | None = None
        self.ui.on_text_command = self._on_text_command
        self.ui.on_interrupt = lambda: None
        self.ui.on_sleep_requested = self.request_sleep
        self.ui.on_wake_requested = self.request_wake
        self.ui.on_quit_requested = self.request_quit
        self.ui.on_api_config_saved = self._on_api_saved
        self.ui.on_trading_control = self._on_trading_hud
        get_trading_logger()
        get_agent_debug_logger()
        _orig_write = self.ui.write_log

        def _logged_write(text: str):
            _orig_write(text)
            try:
                tlog(text, "info")
            except Exception:
                pass

        self.ui.write_log = _logged_write  # type: ignore[method-assign]
        set_trading_hud_sink(self.ui.write_log)
        set_mt5_hud_sink(self.ui.write_log)
        set_activity_hud(self.ui.write_log)
        tlog(f"Trading logger ready → {trading_log_path()}")
        tlog(f"MT5 connection log → {mt5_log_path()}")
        get_analysis_logger()
        tlog(f"Analysis log → {analysis_log_path()}")
        set_graph_enabled(graph_enabled())
        try:
            subscribe_status(self._push_agent_status)
        except Exception:
            pass
        self._push_agent_status(agent_snapshot())
        try:
            err = _ensure_mt5()
            start_mt5_keepalive()
            if err:
                tlog(f"MT5: {err}", "warning", hud=True)
            else:
                tlog("MT5 IPC attached — keepalive on")
        except Exception as e:
            tlog(f"MT5 startup: {e}", "error")
        try:
            refresh_hud(self.ui)
        except Exception as e:
            tlog(f"initial HUD: {e}", "warning")
        self.ui.write_log("SYS: Trading desk online (text). " + _help_text())
        self.ui.set_state("LISTENING")

    def _push_agent_status(self, states: dict) -> None:
        try:
            if hasattr(self.ui, "set_agent_status"):
                self.ui.set_agent_status(states)
        except Exception:
            pass

    def _on_api_saved(self) -> None:
        self.ui.write_log("SYS: API key / models saved.")
        self.ui.set_state("LISTENING")

    def request_sleep(self) -> None:
        if self._hud_sleeping:
            return
        self._hud_sleeping = True
        try:
            set_paused(True, persist=False)
        except Exception:
            pass
        try:
            self.ui.hide_to_tray()
        except Exception:
            pass
        self.ui.set_state("SLEEPING")
        self.ui.write_log("SYS: Sleeping — use the tray icon to wake. Auto-trade paused.")
        if self._loop and self._wake_event is not None:
            self._loop.call_soon_threadsafe(self._wake_event.clear)

    def request_wake(self) -> None:
        was = self._hud_sleeping
        self._hud_sleeping = False
        try:
            set_paused(False, persist=False)
        except Exception:
            pass
        try:
            self.ui.show_from_tray()
        except Exception:
            pass
        if was:
            self.ui.set_state("LISTENING")
            self.ui.write_log("SYS: Back on the desk.")
        if self._loop and self._wake_event is not None:
            self._loop.call_soon_threadsafe(self._wake_event.set)

    def request_quit(self) -> None:
        self.ui.write_log("SYS: Quit requested from tray.")
        os._exit(0)

    def _begin_shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        try:
            set_paused(True, persist=False)
        except Exception:
            pass
        self.ui.write_log("SYS: Shutdown requested.")
        tlog("SYS: Trading shutdown complete.")
        os._exit(0)

    def _on_text_command(self, text: str) -> None:
        if classify_exit_intent(text) == "shutdown":
            self._begin_shutdown()
            return
        if not self._loop:
            self._handle_text_sync(text)
            return
        asyncio.run_coroutine_threadsafe(self._handle_text(text), self._loop)

    def _on_trading_hud(self, action: str) -> None:
        act = (action or "").strip().lower()
        if act == "analyze":
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._run_analysis(), self._loop)
            else:
                self._run_analysis_sync()
            return
        if not self._loop:
            result = trading_control({"action": act}, player=self.ui)
            self.ui.write_log(f"SYS: {str(result)[:240]}")
            return
        asyncio.run_coroutine_threadsafe(self._hud_control(act), self._loop)

    async def _hud_control(self, action: str) -> None:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: trading_control({"action": action}, player=self.ui)
        )
        self.ui.write_log(f"SYS: {str(result)[:240]}")

    def _default_symbol_tf(self) -> tuple[str, str]:
        cfg = load_trading_config()
        symbol = str((cfg.get("symbols") or ["EURUSD"])[0])
        tf = str(cfg.get("timeframe") or "H1")
        return symbol, tf

    def _parse_analyze(self, text: str) -> tuple[str, str] | None:
        raw = (text or "").strip()
        low = raw.lower()
        if not re.match(r"^(analyze|analyse|review|assess)\b", low):
            return None
        rest = re.sub(r"^(analyze|analyse|review|assess)\s*", "", raw, flags=re.I).strip()
        symbol, tf = self._default_symbol_tf()
        parts = rest.split()
        if parts:
            symbol = parts[0].upper()
        if len(parts) >= 2:
            tf = parts[1]
        return symbol, tf

    async def _handle_text(self, text: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._handle_text_sync(text))

    def _handle_text_sync(self, text: str) -> None:
        raw = (text or "").strip()
        if not raw:
            return
        low = raw.lower().strip()
        parsed = self._parse_analyze(raw)
        if parsed:
            self._run_analysis_sync(parsed[0], parsed[1])
            return
        if low in ("pause", "stop auto", "stop trading"):
            msg = trading_control({"action": "pause"}, player=self.ui)
            self.ui.write_log(f"SYS: {msg}")
            return
        if low in ("resume", "start auto"):
            msg = trading_control({"action": "resume"}, player=self.ui)
            self.ui.write_log(f"SYS: {msg}")
            return
        if low in ("flatten", "close all", "flat"):
            msg = trading_control({"action": "flatten"}, player=self.ui)
            self.ui.write_log(f"SYS: {msg}")
            return
        if low in ("status", "desk status"):
            msg = trading_control({"action": "status"}, player=self.ui)
            self.ui.write_log(f"SYS: {msg}")
            return
        if low in ("sleep", "go to sleep"):
            self.request_sleep()
            return
        if low in ("help", "?"):
            self.ui.write_log("SYS: " + _help_text())
            return
        if re.match(r"^(run desk|trade|desk)\b", low):
            cfg_sym, cfg_tf = self._default_symbol_tf()
            parts = raw.split()
            symbol = cfg_sym
            for tok in parts[1:]:
                up = tok.upper()
                if up in {"DESK", "TRADE", "RUN", "ONLY"}:
                    continue
                if len(up) >= 6 and up.isalpha():
                    symbol = up
                    break
            card = trading_desk(
                {"symbol": symbol, "timeframe": cfg_tf, "from_agents": True},
                player=self.ui,
            )
            self.ui.write_log("SYS: " + str(card).splitlines()[0][:200])
            return
        if low.startswith("quote"):
            parts = raw.split()
            symbol = parts[1] if len(parts) > 1 else self._default_symbol_tf()[0]
            msg = mt5_analysis({"action": "quote", "symbol": symbol}, player=self.ui)
            self.ui.write_log(f"SYS: {msg}")
            return
        self._chat_sync(raw)

    def _chat_sync(self, text: str) -> None:
        if not get_gemini_key():
            self.ui.write_log("ERR: Save a Gemini API key in Settings to chat with Athena.")
            return
        self.ui.set_state("THINKING")
        set_status("athena", "busy")
        try:
            from agents.chat import reply
            answer = reply(text)
            if answer:
                self.ui.write_log(f"{self._asst_name}: {answer}")
        except RuntimeError as e:
            self.ui.write_log(f"SYS: {e}")
        except Exception as e:
            tlog(f"chat: {e}", "error")
            traceback.print_exc()
            self.ui.write_log("ERR: Athena could not reply. Check the Gemini key and try again.")
        finally:
            set_status("athena", "ready")
            if not self._hud_sleeping:
                self.ui.set_state("LISTENING")

    async def _run_analysis(self, symbol: str | None = None, timeframe: str | None = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._run_analysis_sync(symbol, timeframe))

    def _run_analysis_sync(self, symbol: str | None = None, timeframe: str | None = None) -> None:
        if not get_gemini_key():
            self.ui.write_log("ERR: Save a Gemini API key in Settings before analysis.")
            return
        ds, dt = self._default_symbol_tf()
        symbol = (symbol or ds).strip()
        timeframe = (timeframe or dt).strip()
        self.ui.set_state("THINKING")
        set_status("athena", "busy")
        try:
            from agents.orchestrator import run_market_analysis
            from agents.conflict import trade_side
            from actions.trading_desk import is_paused, trading_desk
            assessment = run_market_analysis(symbol, timeframe)
            try:
                refresh_hud(self.ui)
            except Exception:
                pass
            if assessment.narrative:
                self.ui.write_log("SYS: " + assessment.narrative[:500])
            side = trade_side(assessment)
            if side in ("BUY", "SELL") and not is_paused():
                self.ui.write_log(
                    f"SYS: Agents say {side} ({assessment.overall}, "
                    f"conf={assessment.confidence:.0%}) — placing demo order…"
                )
                card = trading_desk(
                    {"symbol": symbol, "timeframe": timeframe, "from_agents": True},
                    player=self.ui,
                )
                self.ui.write_log("SYS: " + str(card).splitlines()[0][:240])
            elif side in ("BUY", "SELL") and is_paused():
                self.ui.write_log(
                    f"SYS: Agents say {side}, but auto-trade is paused. "
                    "Press RESUME (or type resume) to allow orders."
                )
            else:
                self.ui.write_log(
                    f"SYS: Agents overall={assessment.overall} "
                    f"conf={assessment.confidence:.0%} → no order ({side})."
                )
        except RuntimeError as e:
            self.ui.write_log(f"SYS: {e}")
        except Exception as e:
            tlog(f"analysis: {e}", "error")
            traceback.print_exc()
            self.ui.write_log("ERR: Market analysis failed. See the activity log.")
        finally:
            set_status("athena", "ready")
            if not self._hud_sleeping:
                self.ui.set_state("LISTENING")

    async def _watch_loop(self):
        cfg = load_trading_config()
        interval = max(8, int(cfg.get("watch_interval_sec") or 20))
        tlog(f"WATCH loop started interval={interval}s symbols={cfg.get('symbols')} tf={cfg.get('timeframe')}")
        while not self._shutdown_started:
            if not self._hud_sleeping:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: watch_tick(self.ui)
                    )
                except Exception as e:
                    tlog(f"watch_tick: {e}", "error")
            await asyncio.sleep(interval)

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._sleep_request = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._wake_event.set()
        activity("Athena trading desk is ready.")
        while not self._shutdown_started:
            if self._hud_sleeping:
                self.ui.set_state("SLEEPING")
                self._wake_event.clear()
                await self._wake_event.wait()
                continue
            try:
                await self._watch_loop()
            except asyncio.CancelledError:
                return
            except Exception:
                traceback.print_exc()
                await asyncio.sleep(3)


def main():
    if getattr(sys, "frozen", False):
        os.chdir(BASE_DIR)
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "Athena.Trading"
                )
            except Exception:
                pass

    face = None
    for candidate in (
        BASE_DIR / "config" / "athena.png",
        BASE_DIR / "config" / "athena.ico",
        BASE_DIR / "face.png",
        BASE_DIR / "config" / "Athena.ico",
    ):
        if candidate.exists():
            face = candidate
            break
    ui = AthenaUI(str(face) if face else "face.png", trading_mode=True)

    def runner():
        ui.wait_for_api_key()
        try:
            from security.keystore import migrate_plaintext
            migrate_plaintext()
        except Exception:
            pass
        session = TradingSession(ui)
        try:
            asyncio.run(session.run())
        except KeyboardInterrupt:
            print("\nShutting down trading desk...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
