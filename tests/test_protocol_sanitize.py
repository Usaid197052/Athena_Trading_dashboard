from __future__ import annotations

import pytest

from agents.protocol import AgentResult, Bias, ExecutionStatus
from security.sanitize import sanitize, sanitize_obj


def test_agent_result_accepts_valid():
    r = AgentResult(
        agent="fundamental",
        task="fundamental_analysis",
        confidence=0.5,
        bias="bullish",
        execution_status="ok",
        model="deepseek-r1:7b",
        analysis="ok",
        reasoning_summary="ok",
    )
    assert r.is_usable()
    assert r.bias == Bias.BULLISH


def test_agent_result_rejects_bad_confidence():
    with pytest.raises(Exception):
        AgentResult(
            agent="x", task="y", confidence=1.5, execution_status="ok",
        )


def test_bias_aliases():
    r = AgentResult(agent="t", task="t", execution_status="ok", bias="BUY")
    assert r.bias == Bias.BULLISH


def test_invalid_status_via_status_from():
    from agents.protocol import status_from
    assert status_from("nope") == ExecutionStatus.ERROR
    assert status_from("timeout") == ExecutionStatus.TIMEOUT


def test_sanitize_api_key():
    raw = 'gemini_api_key": "AIzaSyDummyKeyValue1234567890" and Bearer abcdefghijklmnop'
    out = sanitize(raw)
    assert "AIzaSyDummyKeyValue1234567890" not in out
    assert "abcdefghijklmnop" not in out
    assert "REDACTED" in out


def test_sanitize_obj_drops_secret_fields():
    obj = sanitize_obj({"gemini_api_key": "secret", "ok": "yes"})
    assert obj["gemini_api_key"] == "REDACTED"
    assert obj["ok"] == "yes"
