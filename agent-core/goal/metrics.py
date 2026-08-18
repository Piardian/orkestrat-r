from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any


@dataclass(frozen=True)
class GoalStageMetrics:
    started_at: str | None
    completed_at: str | None
    duration_seconds: float | None
    status: str
    attempts: int = 0
    failure_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GoalMetrics:
    goal_id: str
    state: str
    created_at: str
    updated_at: str
    duration_seconds: float
    llm: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, Any] = field(default_factory=dict)
    builder: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    complexity: dict[str, Any] = field(default_factory=dict)
    estimated_cost: str | float = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stages"] = {
            name: stage.to_dict() if isinstance(stage, GoalStageMetrics) else stage
            for name, stage in self.stages.items()
        }
        return payload

