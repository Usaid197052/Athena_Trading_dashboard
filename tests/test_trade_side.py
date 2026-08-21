from __future__ import annotations

from agents.conflict import trade_side
from agents.protocol import AgentResult, Bias, ExecutionStatus, OverallAssessment
from agents.state import set_last


def test_trade_side_requires_ta_and_floor():
    set_last(None)
    a = OverallAssessment(
        overall="Bullish Bias",
        confidence=0.4,
        symbol="EURUSD",
        timeframe="H1",
        technical=AgentResult(
            agent="technical", task="technical_analysis",
            execution_status=ExecutionStatus.OK, bias=Bias.BULLISH, confidence=0.4,
            engine_score=1, engine_bias="WAIT",
        ),
    )
    # Low confidence and weak engine → WAIT
    assert trade_side(a) == "WAIT"

    # Same bullish overall, but engine score already agrees → BUY (TA leads)
    a.technical.engine_score = 2
    a.technical.engine_bias = "BUY"
    assert trade_side(a) == "BUY"

    a.confidence = 0.6
    a.technical.engine_score = 0
    assert trade_side(a) == "BUY"

    a.technical = None
    a.overall = "Bullish Bias"
    a.fundamental = AgentResult(
        agent="fundamental", task="fundamental_analysis",
        execution_status=ExecutionStatus.OK, bias=Bias.BULLISH, confidence=0.9,
    )
    assert trade_side(a) == "WAIT"
