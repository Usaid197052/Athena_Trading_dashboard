from __future__ import annotations

from agents.sandbox import run_job, write_job


SMA_PLUGIN = """
import numpy as np

def main():
    x = np.arange(1, 21, dtype=float)
    return float(np.mean(x[-5:]))
"""

BLOCKED_PLUGIN = """
import os

def main():
    return os.getcwd()
"""


def test_sandbox_sma_matches_numpy():
    job = write_job(SMA_PLUGIN)
    proc = run_job(job, timeout_sec=15)
    assert proc.returncode == 0, proc.stderr
    assert "18.0" in (proc.stdout or "")


def test_sandbox_blocks_os_import():
    job = write_job(BLOCKED_PLUGIN)
    proc = run_job(job, timeout_sec=15)
    assert proc.returncode != 0
    err = (proc.stderr or "") + (proc.stdout or "")
    assert "blocked" in err.lower() or "SANDBOX_ERROR" in err
