from __future__ import annotations

from types import SimpleNamespace

from agents import chat


def test_reply_uses_flash_and_keeps_history(monkeypatch):
    chat.clear_history()
    monkeypatch.setattr("agents.chat._snapshot", lambda: "ACCOUNT demo EURUSD BUY open")
    monkeypatch.setattr("memory.config_manager.get_gemini_key", lambda: "AIzaSyFakeKeyForTest")
    monkeypatch.setattr("core.gemini_models.get_flash_model", lambda: "gemini-3.6-flash")

    captured: dict = {}

    class _Models:
        def generate_content(self, model, contents):
            captured["model"] = model
            captured["contents"] = contents
            return SimpleNamespace(text="EURUSD is already in a demo BUY. I will not place another order.")

    class _Client:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.models = _Models()

    monkeypatch.setattr("google.genai.Client", _Client)
    out = chat.reply("should we buy more?")
    assert "will not place" in out.lower() or "BUY" in out
    assert captured["model"] == "gemini-3.6-flash"
    assert "DESK SNAPSHOT" in captured["contents"]
    assert "should we buy more?" in captured["contents"]
    assert "order_send" in captured["contents"]
    assert "HOW CONFIDENT" in captured["contents"]
    chat.clear_history()


def test_confidence_brief_explains_disagreement():
    from agents.conflict import OVERALL_NEUTRAL_UNCERTAIN
    from agents.protocol import AgentResult, Bias, ExecutionStatus, OverallAssessment
    from agents.state import format_confidence_brief, set_last

    set_last(OverallAssessment(
        overall=OVERALL_NEUTRAL_UNCERTAIN,
        confidence=0.4,
        disagreement=True,
        why="Fundamental analysis is bullish while technical analysis is bearish.",
        recommendation="Wait for confirmation.",
        symbol="EURUSD",
        timeframe="H1",
        fundamental=AgentResult(
            agent="fundamental", task="fundamental_analysis",
            execution_status=ExecutionStatus.OK, bias=Bias.BULLISH,
            confidence=0.7, reasoning_summary="news is supportive",
        ),
        technical=AgentResult(
            agent="technical", task="technical_analysis",
            execution_status=ExecutionStatus.OK, bias=Bias.BEARISH,
            confidence=0.65, engine_score=2, engine_bias="BUY",
            agrees_with_engine=False, reasoning_summary="momentum fading",
        ),
    ))
    brief = format_confidence_brief()
    assert "Combined confidence: 40%" in brief
    assert "Specialists disagree: YES" in brief
    assert "TRADE CONVICTION: LOW" in brief
    assert "Technical analysis leads" in brief
    assert "Trade side from rules: WAIT" in brief
    set_last(None)
