from __future__ import annotations

from agents.conflict import (
    OVERALL_BEAR,
    OVERALL_BULL,
    OVERALL_INSUFFICIENT,
    OVERALL_NEUTRAL_UNCERTAIN,
    OVERALL_STRONG_BEAR,
    OVERALL_STRONG_BULL,
    resolve,
    trade_side,
)
from agents.protocol import AgentResult, Bias, ExecutionStatus


def _fa(**kw) -> AgentResult:
    base = dict(
        agent="fundamental", task="fundamental_analysis", model="deepseek-r1:7b",
        execution_status=ExecutionStatus.OK, bias=Bias.BULLISH, confidence=0.7,
        data_quality=0.7, event_severity="low", recency_sec=3600,
        reasoning_summary="news",
    )
    base.update(kw)
    return AgentResult(**base)


def _ta(**kw) -> AgentResult:
    base = dict(
        agent="technical", task="technical_analysis", model="qwen2.5-coder:7b",
        execution_status=ExecutionStatus.OK, bias=Bias.BULLISH, confidence=0.7,
        data_quality=0.7, engine_score=3, engine_bias="BUY", agrees_with_engine=True,
        reasoning_summary="ta",
    )
    base.update(kw)
    return AgentResult(**base)


def test_both_missing():
    a = resolve(symbol="EURUSD", timeframe="H1", data_timestamp="t",
                fundamental=None, technical=None)
    assert a.overall == OVERALL_INSUFFICIENT
    assert a.confidence == 0
    assert trade_side(a) == "WAIT"


def test_fa_only_never_trades():
    a = resolve(symbol="EURUSD", timeframe="H1", data_timestamp="t",
                fundamental=_fa(), technical=None)
    assert "Strong" not in a.overall
    assert "technical" in a.layers_missing
    assert trade_side(a) == "WAIT"


def test_agree_strong_bull():
    a = resolve(
        symbol="EURUSD", timeframe="H1", data_timestamp="t",
        fundamental=_fa(confidence=0.8, data_quality=0.8),
        technical=_ta(engine_score=4, confidence=0.8),
    )
    assert a.overall == OVERALL_STRONG_BULL
    assert a.disagreement is False
    assert trade_side(a) == "BUY"


def test_neutral_fa_follows_ta():
    a = resolve(
        symbol="EURUSD", timeframe="H1", data_timestamp="t",
        fundamental=_fa(bias=Bias.NEUTRAL, confidence=0.4, event_severity="noise"),
        technical=_ta(bias=Bias.BULLISH, engine_score=3, confidence=0.75),
    )
    assert a.overall in (OVERALL_BULL, OVERALL_STRONG_BULL)
    assert a.disagreement is False
    assert trade_side(a) == "BUY"


def test_weak_fa_opposite_ta_still_follows_ta():
    a = resolve(
        symbol="EURUSD", timeframe="H1", data_timestamp="t",
        fundamental=_fa(bias=Bias.BULLISH, event_severity="low", confidence=0.6),
        technical=_ta(bias=Bias.BEARISH, engine_score=-2, engine_bias="SELL", confidence=0.7),
    )
    assert a.overall == OVERALL_BEAR
    assert a.disagreement is True
    assert trade_side(a) == "BUY" or trade_side(a) == "SELL" or trade_side(a) == "WAIT"
    # confidence capped at 0.55 for disagreement; MIN_TRADE is 0.50 so SELL if conf ok
    assert trade_side(a) == "SELL"


def test_high_impact_fa_vetoes_weak_ta():
    a = resolve(
        symbol="EURUSD", timeframe="H1", data_timestamp="t",
        fundamental=_fa(
            bias=Bias.BULLISH, event_severity="high", recency_sec=1000, confidence=0.8,
        ),
        technical=_ta(bias=Bias.BEARISH, engine_score=-1, engine_bias="WAIT", confidence=0.5),
    )
    assert a.disagreement is True
    assert a.overall == OVERALL_NEUTRAL_UNCERTAIN
    assert trade_side(a) == "WAIT"


def test_noise_fa_lets_strong_ta_lead():
    a = resolve(
        symbol="EURUSD", timeframe="H1", data_timestamp="t",
        fundamental=_fa(bias=Bias.BULLISH, event_severity="noise", confidence=0.3),
        technical=_ta(bias=Bias.BEARISH, engine_score=-4, engine_bias="SELL", confidence=0.8),
    )
    assert a.disagreement is False
    assert a.overall == OVERALL_STRONG_BEAR
    assert trade_side(a) == "SELL"
