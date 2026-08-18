from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [
            str(item).strip()
            for item in value
            if str(item).strip()
        ]
    raise ValueError(f"Expected a string or a list of strings, got {type(value)}")


@dataclass(frozen=True)
class GoalPlan:
    plan_version: int
    goal_id: str
    objective: str
    summary: str
    tasks: list[dict[str, Any]]
    candidate_files: list[str]
    acceptance_criteria: list[str]
    verification: list[str]
    risks: list[str]
    constraints: list[str]
    patch_expected: bool
    uncertainties: list[str]
    evidence_refs: list[str]
    status: str = "PLANNED"
    provider_requests: int = 0
    logical_calls: int = 0
    provider_retries: int = 0
    json_repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    evidence_size: int = 0
    allowed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GoalPlan":
        return cls(
            plan_version=int(raw.get("plan_version", 1) or 1),
            goal_id=str(raw.get("goal_id", "")),
            objective=str(raw.get("objective", "")),
            summary=str(raw.get("summary", "")),
            tasks=[item for item in raw.get("tasks", []) if isinstance(item, dict)],
            candidate_files=normalize_string_list(raw.get("candidate_files")),
            allowed_files=normalize_string_list(raw.get("allowed_files")),
            acceptance_criteria=normalize_string_list(raw.get("acceptance_criteria")),
            verification=normalize_string_list(raw.get("verification")),
            risks=normalize_string_list(raw.get("risks")),
            constraints=normalize_string_list(raw.get("constraints")),
            patch_expected=bool(raw.get("patch_expected", True)),
            uncertainties=normalize_string_list(raw.get("uncertainties")),
            evidence_refs=normalize_string_list(raw.get("evidence_refs")),
            status=str(raw.get("status", "PLANNED")),
            provider_requests=int(raw.get("provider_requests", 0) or 0),
            logical_calls=int(raw.get("logical_calls", 0) or 0),
            provider_retries=int(raw.get("provider_retries", 0) or 0),
            json_repairs=int(raw.get("json_repairs", 0) or 0),
            input_tokens=int(raw.get("input_tokens", 0) or 0),
            output_tokens=int(raw.get("output_tokens", 0) or 0),
            evidence_size=int(raw.get("evidence_size", 0) or 0),
        )
