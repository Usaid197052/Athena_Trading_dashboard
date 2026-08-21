"""Deterministic conflict resolution. Technical analysis leads; FA can veto."""
from __future__ import annotations

from datetime import datetime, timezone

from agents.protocol import AgentResult, Bias, ExecutionStatus, OverallAssessment

_DIR_POS = {Bias.BULLISH}
_DIR_NEG = {Bias.BEARISH}

OVERALL_STRONG_BULL = "Strong Bullish Bias"
OVERALL_BULL = "Bullish Bias"
OVERALL_NEUTRAL = "Neutral"
OVERALL_BEAR = "Bearish Bias"
OVERALL_STRONG_BEAR = "Strong Bearish Bias"
OVERALL_INSUFFICIENT = "Insufficient Data"
OVERALL_NEUTRAL_UNCERTAIN = "Neutral / High Uncertainty"

MIN_TRADE_CONFIDENCE = 0.50


def _usable(r: AgentResult | None) -> AgentResult | None:
    if r is None:
        return None
    if r.execution_status != ExecutionStatus.OK:
        return None
    if r.bias == Bias.INSUFFICIENT:
        return None
    return r


def _direction(bias: Bias) -> str:
    if bias in _DIR_POS:
        return "bull"
    if bias in _DIR_NEG:
        return "bear"
    return "flat"


def _fresh(fa: AgentResult) -> bool:
    """recency_sec == 0 means unknown — only treat as fresh when severity is high."""
    if int(fa.recency_sec or 0) <= 0:
        return (fa.event_severity or "").lower() == "high"
    return int(fa.recency_sec) < 86400


def _high_fa(fa: AgentResult) -> bool:
    sev = (fa.event_severity or "").lower()
    if sev == "high":
        return True
    if sev == "material" and _fresh(fa):
        return True
    counts = fa.news_counts or {}
    return int(counts.get("material") or 0) >= 2 and fa.confidence >= 0.6


def _ta_strong(ta: AgentResult) -> bool:
    return abs(int(ta.engine_score or 0)) >= 3 and ta.confidence >= 0.55


def _fa_near_neutral(fa: AgentResult | None) -> bool:
    """Weak/absent news should not block a technical setup."""
    if fa is None:
        return True
    if fa.bias in (Bias.NEUTRAL, Bias.INSUFFICIENT):
        return True
    sev = (fa.event_severity or "").lower()
    if fa.confidence < 0.45:
        return True
    if sev in {"noise", "low", "unknown", ""} and fa.confidence < 0.55:
        return True
    return False


def _ta_label(ta: AgentResult, *, strong_ok: bool) -> str:
    d = _direction(ta.bias)
    if d == "flat":
        return OVERALL_NEUTRAL
    if strong_ok and _ta_strong(ta):
        return OVERALL_STRONG_BULL if d == "bull" else OVERALL_STRONG_BEAR
    return OVERALL_BULL if d == "bull" else OVERALL_BEAR


def trade_side(assessment: OverallAssessment) -> str:
    """Map an assessment to BUY/SELL/WAIT. Requires usable TA. Never FA-only.

    Allows a trade when overall is directional and either combined confidence
    clears the floor, or the built-in engine score already agrees (|score|>=2).
    """
    if assessment is None:
        return "WAIT"
    ta = assessment.usable_technical()
    if ta is None:
        return "WAIT"
    overall = assessment.overall or ""
    conf = float(assessment.confidence or 0)
    score = int(ta.engine_score or 0)
    if overall in (OVERALL_STRONG_BULL, OVERALL_BULL):
        if conf >= MIN_TRADE_CONFIDENCE or score >= 2:
            return "BUY"
        return "WAIT"
    if overall in (OVERALL_STRONG_BEAR, OVERALL_BEAR):
        if conf >= MIN_TRADE_CONFIDENCE or score <= -2:
            return "SELL"
        return "WAIT"
    return "WAIT"


def resolve(
    *,
    symbol: str,
    timeframe: str,
    data_timestamp: str,
    fundamental: AgentResult | None,
    technical: AgentResult | None,
    graph: AgentResult | None = None,
    bar_time: int = 0,
) -> OverallAssessment:
    fa = _usable(fundamental)
    ta = _usable(technical)
    missing: list[str] = []
    if fundamental is None or _usable(fundamental) is None:
        missing.append("fundamental")
    if technical is None or _usable(technical) is None:
        missing.append("technical")

    risks: list[str] = []
    warnings: list[str] = []
    for r in (fundamental, technical, graph):
        if not r:
            continue
        risks.extend(r.risks or [])
        warnings.extend(r.warnings or [])

    ts = datetime.now(timezone.utc).isoformat()
    base = dict(
        fundamental=fundamental,
        technical=technical,
        graph=graph,
        layers_missing=missing,
        timestamp=ts,
        data_timestamp=data_timestamp,
        symbol=symbol,
        timeframe=timeframe,
        risks=risks[:8],
        warnings=warnings[:8],
        bar_time=int(bar_time or 0),
    )

    if fa is None and ta is None:
        return OverallAssessment(
            overall=OVERALL_INSUFFICIENT,
            confidence=0.0,
            disagreement=False,
            why="Neither fundamental nor technical analysis produced a usable result.",
            recommendation="Wait. Do not treat a missing analysis as a trading signal.",
            **base,
        )

    if ta is None:
        label = {
            Bias.BULLISH: OVERALL_BULL,
            Bias.BEARISH: OVERALL_BEAR,
            Bias.NEUTRAL: OVERALL_NEUTRAL,
        }.get(fa.bias, OVERALL_NEUTRAL)
        return OverallAssessment(
            overall=label,
            confidence=min(0.45, float(fa.confidence or 0)),
            disagreement=False,
            why=(
                "Fundamental analysis is available but technical analysis is not. "
                "Athena will not trade without a technical setup."
            ),
            recommendation="Wait for technical analysis. News alone is not an order.",
            **base,
        )

    if fa is None or _fa_near_neutral(fa):
        overall = _ta_label(ta, strong_ok=_ta_strong(ta))
        why = (
            "Technical analysis is directional. Fundamental analysis is missing, "
            "neutral, or too weak to override the chart."
        )
        if _direction(ta.bias) == "flat":
            why = "Technical analysis is not directional. Fundamental input is not enough to force a trade."
        rec = (
            "Athena follows the technical setup. News is noted but not driving the decision."
            if _direction(ta.bias) != "flat"
            else "No technical edge. Wait."
        )
        return OverallAssessment(
            overall=overall,
            confidence=min(0.85, float(ta.confidence or 0)),
            disagreement=False,
            why=why,
            recommendation=rec,
            **base,
        )

    same = _direction(fa.bias) == _direction(ta.bias) and _direction(fa.bias) != "flat"
    both_flat = _direction(fa.bias) == "flat" and _direction(ta.bias) == "flat"

    if both_flat:
        return OverallAssessment(
            overall=OVERALL_NEUTRAL,
            confidence=min(fa.confidence, ta.confidence),
            disagreement=False,
            why="Both specialists are neutral on direction.",
            recommendation="No directional edge. Wait for a clearer setup.",
            **base,
        )

    if same:
        bull = _direction(ta.bias) == "bull"
        quality_ok = (fa.data_quality >= 0.45 or fa.confidence >= 0.55) and (
            ta.data_quality >= 0.45 or ta.confidence >= 0.55
        )
        if quality_ok and _ta_strong(ta) and fa.confidence >= 0.55:
            overall = OVERALL_STRONG_BULL if bull else OVERALL_STRONG_BEAR
            why = (
                "Technical setup is clear and fundamental analysis agrees. "
                "Athena weights the chart more than the news."
            )
            rec = "They agree. Demo auto-trade may act on this if it is enabled."
        else:
            overall = OVERALL_BULL if bull else OVERALL_BEAR
            why = (
                "Technical and fundamental analysis point the same way, "
                "with technical analysis leading."
            )
            rec = "Directional bias. Demo auto-trade may act if confidence clears the floor."
        conf = min(0.9, 0.65 * float(ta.confidence) + 0.35 * float(fa.confidence))
        return OverallAssessment(
            overall=overall,
            confidence=conf,
            disagreement=False,
            why=why,
            recommendation=rec,
            **base,
        )

    # Opposite direction: high-impact fresh news can veto TA; otherwise TA wins.
    if _high_fa(fa) and _fresh(fa) and not _ta_strong(ta):
        return OverallAssessment(
            overall=OVERALL_NEUTRAL_UNCERTAIN,
            confidence=min(0.45, float(ta.confidence or 0)),
            disagreement=True,
            why=(
                f"Technical analysis is {ta.bias.value} but recent high-impact news is "
                f"{fa.bias.value}. Athena will not fight that news."
            ),
            recommendation="Wait. High-impact fundamentals veto a weak or moderate technical signal.",
            **base,
        )

    overall = _ta_label(ta, strong_ok=False)
    return OverallAssessment(
        overall=overall,
        confidence=min(0.55, float(ta.confidence or 0)),
        disagreement=True,
        why=(
            f"Technical analysis is {ta.bias.value} while fundamental analysis is "
            f"{fa.bias.value}. Athena still follows the chart because the news is "
            "not a high-impact veto."
        ),
        recommendation=(
            "Prioritising technical analysis. Size down or wait if you disagree with the news read."
        ),
        **base,
    )
