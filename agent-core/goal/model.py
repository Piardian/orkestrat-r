from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class GoalRecord:
    goal_id: str
    goal: str
    repo: str
    status: str
    created_at: str
    updated_at: str
    phase: str = "intake"
    utc_timestamp: bool = True
    goal_type: str = "CODE_MODIFICATION"
    notes: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload["notes"] is None:
            payload["notes"] = []
        return payload
