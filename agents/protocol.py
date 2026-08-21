"""Pydantic contracts between Athena and worker agents."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Bias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    INSUFFICIENT = "insufficient"


class ExecutionStatus(str, Enum):
    OK = "ok"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    ERROR = "error"
    DISABLED = "disabled"


class AgentResult(BaseModel):
    """Structured worker output. Invalid instances must not reach aggregation as truth."""

    model_config = ConfigDict(extra="allow")

    agent: str
    task: str
    input_data: dict[str, Any] = Field(default_factory=dict)
    analysis: str = ""
    signals: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning_summary: str = ""
    risks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timestamp: str = ""
    model: str = ""
    model_version: str = ""
    execution_status: ExecutionStatus = ExecutionStatus.ERROR
    bias: Bias = Bias.INSUFFICIENT
    facts: list[str] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)
    data_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    recency_sec: int = Field(default=0, ge=0)
    event_severity: str = "unknown"
    engine_score: int = 0
    engine_bias: str = ""
    agrees_with_engine: bool = True
    indicator_agreement: float = Field(default=0.0, ge=0.0, le=1.0)
    news_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _ts(cls, v: Any) -> str:
        if v:
            return str(v)
        return datetime.now(timezone.utc).isoformat()

    @field_validator("bias", mode="before")
    @classmethod
    def _bias(cls, v: Any) -> Any:
        if v is None or v == "":
            return Bias.INSUFFICIENT
        if isinstance(v, Bias):
            return v
        s = getattr(v, "value", v)
        s = str(s).strip().lower()
        if s.startswith("bias."):
            s = s.split(".", 1)[-1]
        aliases = {
            "buy": "bullish",
            "sell": "bearish",
            "wait": "neutral",
            "strong bullish": "bullish",
            "strong bearish": "bearish",
        }
        return aliases.get(s, s)

    @field_validator("execution_status", mode="before")
    @classmethod
    def _st(cls, v: Any) -> Any:
        if v is None or v == "":
            return ExecutionStatus.ERROR
        if isinstance(v, ExecutionStatus):
            return v
        s = getattr(v, "value", v)
        s = str(s).strip().lower()
        if s.startswith("executionstatus."):
            s = s.split(".", 1)[-1]
        return s

    @field_validator("signals", "risks", "warnings", "facts", "interpretations", "scenarios", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return list(v)

    def is_usable(self) -> bool:
        return self.execution_status == ExecutionStatus.OK and self.bias != Bias.INSUFFICIENT


class OverallAssessment(BaseModel):
    overall: str
    confidence: float = Field(ge=0.0, le=1.0)
    disagreement: bool = False
    why: str = ""
    recommendation: str = ""
    fundamental: AgentResult | None = None
    technical: AgentResult | None = None
    graph: AgentResult | None = None
    layers_missing: list[str] = Field(default_factory=list)
    timestamp: str = ""
    data_timestamp: str = ""
    symbol: str = ""
    timeframe: str = ""
    risks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    narrative: str = ""
    bar_time: int = 0

    def usable_fundamental(self) -> AgentResult | None:
        if self.fundamental and self.fundamental.execution_status == ExecutionStatus.OK:
            return self.fundamental
        return None

    def usable_technical(self) -> AgentResult | None:
        if self.technical and self.technical.execution_status == ExecutionStatus.OK:
            return self.technical
        return None


def status_from(name: str) -> ExecutionStatus:
    try:
        return ExecutionStatus(str(name).strip().lower())
    except Exception:
        return ExecutionStatus.ERROR


def failed_result(
    agent: str,
    task: str,
    model: str,
    status: ExecutionStatus,
    warning: str,
    **extra: Any,
) -> AgentResult:
    return AgentResult(
        agent=agent,
        task=task,
        model=model,
        execution_status=status,
        bias=Bias.INSUFFICIENT,
        warnings=[warning],
        analysis=warning,
        reasoning_summary=warning,
        timestamp=datetime.now(timezone.utc).isoformat(),
        **extra,
    )
