"""
Local wake-word listener for Athena sleep mode.

Uses Vosk with a constrained grammar so the mic only listens for
"hey {assistant_name}" while the HUD is hidden and Gemini is paused.
"""
from __future__ import annotations

import json
import re
import threading
import zipfile
from pathlib import Path
from typing import Callable
from urllib.request import urlretrieve

import numpy as np

SAMPLE_RATE = 16000
CHUNK_SIZE = 1024
MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = (
    "https://alphacephei.com/vosk/models/"
    f"{MODEL_NAME}.zip"
)


def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def model_dir() -> Path:
    return _base_dir() / "models" / "vosk-small-en-us"


def ensure_model(log: Callable[[str], None] | None = None) -> Path:
    """Download and extract the small Vosk English model if missing."""
    dest = model_dir()
    marker = dest / "am" / "final.mdl"
    if marker.exists():
        return dest

    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    zip_path = parent / f"{MODEL_NAME}.zip"
    extracted = parent / MODEL_NAME

    if log:
        log("SYS: Downloading wake-word model (one-time, ~40 MB)…")
    try:
        urlretrieve(MODEL_URL, str(zip_path))
    except Exception as e:
        raise RuntimeError(f"Wake-word model download failed: {e}") from e

    if log:
        log("SYS: Extracting wake-word model…")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(parent)

    # Zip extracts as models/vosk-model-small-en-us-0.15/ — move to models/vosk-small-en-us/
    if extracted.exists():
        if dest.exists():
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
        extracted.rename(dest)
    try:
        zip_path.unlink(missing_ok=True)
    except Exception:
        pass

    if not (dest / "am" / "final.mdl").exists():
        raise RuntimeError(f"Wake-word model incomplete at {dest}")
    if log:
        log("SYS: Wake-word model ready.")
    return dest


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", (text or "").lower()).strip()


def _is_wake(text: str, assistant_name: str) -> bool:
    t = _normalize(text)
    name = _normalize(assistant_name)
    if not t or not name:
        return False
    # Accept "hey athena", "hey athena please", trailing noise
    if f"hey {name}" in t:
        return True
    # Partial streams sometimes drop "hey" briefly — require both tokens nearby
    parts = t.split()
    if "hey" in parts and name in parts:
        try:
            i = parts.index("hey")
            j = parts.index(name)
            if abs(i - j) <= 2:
                return True
        except ValueError:
            pass
    return False


def _grammar_for(assistant_name: str) -> str:
    name = _normalize(assistant_name) or "athena"
    phrases = [
        f"hey {name}",
        f"hey {name} hey {name}",
        "[unk]",
    ]
    return json.dumps(phrases)


class WakeWordListener:
    """Background mic listener that fires on_wake when the phrase is heard."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._on_wake: Callable[[], None] | None = None
        self._name = "athena"
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    def start(
        self,
        assistant_name: str,
        on_wake: Callable[[], None],
        log: Callable[[str], None] | None = None,
    ) -> None:
        with self._lock:
            if self._running:
                self.stop()
            self._name = (assistant_name or "athena").strip() or "athena"
            self._on_wake = on_wake
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(log,),
                name="WakeWordListener",
                daemon=True,
            )
            self._running = True
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.5)
        with self._lock:
            self._running = False
            self._thread = None
            self._on_wake = None

    def _run(self, log: Callable[[str], None] | None) -> None:
        try:
            import sounddevice as sd
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            path = ensure_model(log)
            model = Model(str(path))
            rec = KaldiRecognizer(model, SAMPLE_RATE, _grammar_for(self._name))
            rec.SetWords(False)
            if log:
                log(f"SYS: Wake-word listening for 'Hey {self._name}'…")

            fired = False

            def callback(indata, frames, time_info, status):
                nonlocal fired
                if self._stop.is_set() or fired:
                    return
                try:
                    pcm = indata.astype(np.int16).tobytes() if indata.dtype != np.int16 else indata.tobytes()
                    text = ""
                    if rec.AcceptWaveform(pcm):
                        text = json.loads(rec.Result()).get("text", "")
                    else:
                        text = json.loads(rec.PartialResult()).get("partial", "")
                    if text and _is_wake(text, self._name):
                        fired = True
                        cb = self._on_wake
                        if cb:
                            try:
                                cb()
                            except Exception as e:
                                if log:
                                    log(f"ERR: Wake callback failed — {e}")
                except Exception:
                    pass

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                while not self._stop.is_set() and not fired:
                    self._stop.wait(0.1)
        except Exception as e:
            if log:
                log(f"ERR: Wake-word listener failed — {e}")
        finally:
            with self._lock:
                self._running = False
