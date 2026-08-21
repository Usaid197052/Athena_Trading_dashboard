"""Last multi-agent assessment, for HUD text (separate from desk LAST)."""
from __future__ import annotations

import threading
from typing import Any

from agents.protocol import OverallAssessment

_lock = threading.Lock()
_last: OverallAssessment | None = None


def set_last(assessment: OverallAssessment | None) -> None:
    global _last
    with _lock:
        _last = assessment


def get_last() -> OverallAssessment | None:
    with _lock:
        return _last


def format_confidence_brief() -> str:
    """Plain-language pack so Athena can explain conviction without inventing it."""
    a = get_last()
    lines = [
        "CONFIDENCE BRIEF",
        "Technical analysis leads. Fundamentals can veto only on high-impact fresh news.",
        "If FA is neutral/weak/missing, Athena follows the chart.",
        "Default symbol is EURUSD unless the owner named another pair.",
        "",
        "1) MULTI-AGENT ANALYSIS — used for chat AND demo auto-trade (after gates).",
    ]
    if a is None:
        lines.append(
            "  No Analyze result yet. Tell the owner to press Analyze or type `analyze`, "
            "then ask again. Do not invent confidence."
        )
    else:
        lines.append(f"  Symbol {a.symbol or '-'} {a.timeframe or '-'}")
        lines.append(f"  Overall label: {a.overall}")
        lines.append(
            f"  Combined confidence: {a.confidence:.0%} "
            "(capped from specialists; not an average of 'truth'; not used to send orders)"
        )
        lines.append(f"  Specialists disagree: {'YES' if a.disagreement else 'no'}")
        if a.why:
            lines.append(f"  Why this label: {a.why}")
        if a.recommendation:
            lines.append(f"  Recommendation: {a.recommendation}")
        if a.layers_missing:
            lines.append("  Missing layers: " + ", ".join(a.layers_missing))
        fa = a.fundamental
        if fa:
            lines.append(
                f"  DeepSeek fundamental: bias={fa.bias.value} "
                f"status={fa.execution_status.value} confidence={fa.confidence:.0%} "
                f"data_quality={fa.data_quality:.0%} severity={fa.event_severity or 'unknown'}"
            )
            if fa.reasoning_summary:
                lines.append(f"    summary: {fa.reasoning_summary[:280]}")
            facts = [str(x) for x in (fa.facts or [])[:4] if x]
            if facts:
                lines.append("    facts: " + "; ".join(facts)[:300])
            interps = [str(x) for x in (fa.interpretations or [])[:3] if x]
            if interps:
                lines.append("    interpretation: " + "; ".join(interps)[:300])
        ta = a.technical
        if ta:
            lines.append(
                f"  Qwen technical: bias={ta.bias.value} "
                f"status={ta.execution_status.value} confidence={ta.confidence:.0%} "
                f"engine_bias={ta.engine_bias or '-'} engine_score={ta.engine_score} "
                f"agrees_with_engine={ta.agrees_with_engine} "
                f"indicator_agreement={ta.indicator_agreement:.0%}"
            )
            if ta.reasoning_summary:
                lines.append(f"    summary: {ta.reasoning_summary[:280]}")
        if a.risks:
            lines.append("  Risks: " + "; ".join(str(r) for r in a.risks[:4])[:280])
        if a.warnings:
            lines.append("  Warnings: " + "; ".join(str(w) for w in a.warnings[:4])[:280])
        if a.narrative:
            lines.append(f"  Last Athena narrative: {a.narrative[:400]}")
        try:
            from agents.conflict import trade_side
            side = trade_side(a)
        except Exception:
            side = "WAIT"
        lines.append(f"  Trade side from rules: {side}")
        if side == "WAIT" or a.confidence < 0.50:
            lines.append(
                "  TRADE CONVICTION: LOW / WAIT. Do not describe this as a confident trade."
            )
        elif "Strong" in (a.overall or "") and not a.disagreement:
            lines.append(
                "  TRADE CONVICTION: HIGHER. Specialists align or FA is too weak to override TA. "
                "Still probabilistic."
            )
        else:
            lines.append(
                "  TRADE CONVICTION: MODERATE. Chart leads; do not oversell certainty."
            )

    lines.extend([
        "",
        "2) DEMO AUTO-TRADE — uses the same agent overall + trade_side rules, then ATR stops and gates.",
        "  Engine score is an input to Qwen, not a separate order path.",
        "  Manual Analyze button alone does not place an order; watch loop / run desk can.",
    ])
    try:
        from actions.trading_desk import last_autotrade_brief
        brief = last_autotrade_brief() or {}
        if brief:
            lines.append(
                f"  Last desk: status={brief.get('status') or '-'} "
                f"bias={brief.get('bias') or '-'} score={brief.get('score') or '-'}"
            )
            if brief.get("why"):
                lines.append(f"    {brief['why'][:240]}")
            if brief.get("gates"):
                lines.append(f"    {brief['gates'][:240]}")
            if brief.get("plan"):
                lines.append(f"    {brief['plan'][:200]}")
            if brief.get("reason"):
                lines.append(f"    result: {brief['reason'][:200]}")
        else:
            lines.append("  No auto-trade scorecard stored yet.")
    except Exception:
        lines.append("  Auto-trade scorecard unavailable.")
    return "\n".join(lines)


def format_hud_analysis() -> str:
    a = get_last()
    if a is None:
        return ""
    lines = ["ANALYSIS  (decision support — does not place orders)"]
    lines.append(
        f"  OVERALL  {a.overall}  confidence={a.confidence:.0%}  "
        f"disagree={'yes' if a.disagreement else 'no'}"
    )
    fa = a.fundamental
    ta = a.technical
    ga = a.graph
    if fa:
        lines.append(
            f"  FUNDAMENTAL  {fa.bias.value}  {fa.execution_status.value}  "
            f"conf={fa.confidence:.0%}  {(fa.reasoning_summary or '')[:160]}"
        )
    if ta:
        lines.append(
            f"  TECHNICAL  {ta.bias.value}  {ta.execution_status.value}  "
            f"conf={ta.confidence:.0%}  engine={ta.engine_bias or '-'}  "
            f"{(ta.reasoning_summary or '')[:140]}"
        )
    if ga and ga.execution_status.value not in ("disabled",):
        lines.append(
            f"  GRAPH  {ga.bias.value}  {ga.execution_status.value}  "
            f"{(ga.reasoning_summary or '')[:140]}"
        )
    if a.layers_missing:
        lines.append("  MISSING  " + ", ".join(a.layers_missing))
    if a.why:
        lines.append(f"  WHY  {a.why[:220]}")
    if a.recommendation:
        lines.append(f"  NOTE  {a.recommendation[:220]}")
    risks = a.risks or []
    if risks:
        lines.append("  RISKS  " + "; ".join(str(r) for r in risks[:3])[:220])
    if a.data_timestamp:
        lines.append(f"  DATA  {a.data_timestamp}")
    return "\n".join(lines)


def assessment_to_dict(a: OverallAssessment) -> dict[str, Any]:
    return a.model_dump(mode="json")
