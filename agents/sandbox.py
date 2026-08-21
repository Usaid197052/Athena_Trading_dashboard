"""Controlled execution of untrusted generated Python (never used on the default analysis path)."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from core.agent_debug_logger import debug, event


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


JOBS = _base_dir() / "sandbox" / "jobs"
APPROVED = _base_dir() / "sandbox" / "approved"
RUNNER = _base_dir() / "sandbox" / "runner.py"

ALLOWED_TOP_LEVEL = frozenset({"numpy", "pandas", "math"})


def new_job_dir() -> Path:
    path = JOBS / uuid.uuid4().hex[:12]
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_job(source: str, *, filename: str = "plugin.py") -> Path:
    job = new_job_dir()
    (job / filename).write_text(source, encoding="utf-8")
    return job


def run_job(
    job_dir: Path,
    *,
    timeout_sec: float = 8.0,
    filename: str = "plugin.py",
) -> subprocess.CompletedProcess[str]:
    if not RUNNER.exists():
        raise FileNotFoundError(f"sandbox runner missing: {RUNNER}")
    env = {
        "PYTHONPATH": str(_base_dir()),
        "ATHENA_SANDBOX": "1",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "PATH": os.environ.get("PATH", ""),
        "TEMP": os.environ.get("TEMP", str(job_dir)),
        "TMP": os.environ.get("TMP", str(job_dir)),
    }
    event("sandbox_start", task_id=job_dir.name)
    proc = subprocess.run(
        [sys.executable, str(RUNNER), str(job_dir / filename)],
        cwd=str(job_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    event("sandbox_done", task_id=job_dir.name, status=str(proc.returncode))
    if proc.returncode != 0:
        debug(f"sandbox failed rc={proc.returncode}", "warning")
    return proc


def promote(job_dir: Path, name: str) -> Path:
    APPROVED.mkdir(parents=True, exist_ok=True)
    dest = APPROVED / name
    src = job_dir / "plugin.py"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest
