from __future__ import annotations

import json
import sys

import pytest

from agents.protocol import AgentResult, Bias, ExecutionStatus
from agents.runtime_ollama import extract_json, strip_think


def test_strip_think_and_extract_json():
    raw = "<think>secret chain</think>{\"agent\": \"fundamental\", \"bias\": \"bullish\"}"
    obj = extract_json(raw)
    assert obj["bias"] == "bullish"
    assert "secret" not in strip_think(raw)


def test_extract_json_fenced():
    raw = "```json\n{\"execution_status\": \"ok\"}\n```"
    assert extract_json(raw)["execution_status"] == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_keystore_roundtrip(tmp_path, monkeypatch):
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({"gemini_api_key": "AIzaSyTestKeyValue999999"}), encoding="utf-8")
    monkeypatch.setattr("memory.config_manager.CONFIG_FILE", cfg)
    monkeypatch.setattr("security.keystore.CONFIG_FILE", cfg)
    from security.keystore import get_active_key, list_keys, migrate_plaintext
    data = migrate_plaintext()
    assert "gemini_api_key" not in data
    assert data.get("gemini_keys")
    assert get_active_key() == "AIzaSyTestKeyValue999999"
    rows = list_keys()
    assert rows[0]["masked"] != "AIzaSyTestKeyValue999999"
    assert "AIza" in rows[0]["masked"] or "…" in rows[0]["masked"]


def test_orchestrator_with_fakes(monkeypatch):
    from agents.conflict import OVERALL_STRONG_BULL
    from agents.orchestrator import run_market_analysis
    from data.snapshot import NormalizedMarketSnapshot

    snap = NormalizedMarketSnapshot(
        symbol="EURUSD", timeframe="H1", ok=True,
        ta={"ok": True, "score": 4, "bias": "BUY", "rsi": 55},
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    fa = AgentResult(
        agent="fundamental", task="fundamental_analysis", model="deepseek-r1:7b",
        execution_status=ExecutionStatus.OK, bias=Bias.BULLISH, confidence=0.8,
        data_quality=0.8, event_severity="low", recency_sec=100,
        reasoning_summary="news supportive",
    )
    ta = AgentResult(
        agent="technical", task="technical_analysis", model="qwen2.5-coder:7b",
        execution_status=ExecutionStatus.OK, bias=Bias.BULLISH, confidence=0.8,
        data_quality=0.8, engine_score=4, engine_bias="BUY",
        reasoning_summary="trend up",
    )
    monkeypatch.setattr("agents.orchestrator.acquire", lambda *a, **k: snap)
    monkeypatch.setattr("agents.orchestrator.run_technical", lambda s: ta)
    monkeypatch.setattr("agents.orchestrator.run_fundamental", lambda s: fa)
    monkeypatch.setattr("agents.orchestrator.graph_enabled", lambda: False)
    monkeypatch.setattr("agents.orchestrator._narrative", lambda a: "Combined in plain language.")
    out = run_market_analysis("EURUSD", "H1")
    assert out.overall == OVERALL_STRONG_BULL
    assert out.narrative.startswith("Combined")
    assert out.disagreement is False


def test_orchestrator_degrades_when_fundamental_fails(monkeypatch):
    from agents.orchestrator import run_market_analysis
    from agents.protocol import failed_result
    from data.snapshot import NormalizedMarketSnapshot

    snap = NormalizedMarketSnapshot(symbol="EURUSD", timeframe="H1", ok=True, ta={"ok": True, "score": 3, "bias": "BUY"})
    ta = AgentResult(
        agent="technical", task="technical_analysis", model="qwen",
        execution_status=ExecutionStatus.OK, bias=Bias.BEARISH, confidence=0.6,
        engine_score=-3, engine_bias="SELL",
    )
    monkeypatch.setattr("agents.orchestrator.acquire", lambda *a, **k: snap)
    monkeypatch.setattr("agents.orchestrator.run_technical", lambda s: ta)
    monkeypatch.setattr(
        "agents.orchestrator.run_fundamental",
        lambda s: failed_result("fundamental", "fundamental_analysis", "deepseek-r1:7b",
                                ExecutionStatus.UNAVAILABLE, "Ollama is not running."),
    )
    monkeypatch.setattr("agents.orchestrator.graph_enabled", lambda: False)
    monkeypatch.setattr("agents.orchestrator._narrative", lambda a: "")
    out = run_market_analysis("EURUSD", "H1")
    assert "technical" not in out.layers_missing
    assert "fundamental" in out.layers_missing
    # Missing/failed FA must not block a clear technical setup.
    assert out.overall.endswith("Bearish Bias")
    from agents.conflict import trade_side
    assert trade_side(out) == "SELL"
