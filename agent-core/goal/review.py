from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


VALID_REVIEW_STATUSES = {"APPROVED", "REVISION_REQUIRED", "REVIEW_UNKNOWN", "REVIEW_FAILED"}


@dataclass(frozen=True)
class GoalReview:
    goal_id: str
    task: str
    reviewer_profile: str
    status: str
    agreement: str
    final_verdict: str
    confidence: float
    reason: str
    patch_required: bool
    analyst_results: list[dict[str, Any]]
    reviewer_result: dict[str, Any]
    evidence_refs: list[str]
    provider_requests: int = 0
    logical_calls: int = 0
    provider_retries: int = 0
    json_repairs: int = 0
    stage_regenerations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    review_429_count: int = 0
    review_503_count: int = 0
    review_timeout_count: int = 0
    provider_health: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GoalReview":
        return cls(
            goal_id=str(raw.get("goal_id", "")),
            task=str(raw.get("task", "")),
            reviewer_profile=str(raw.get("reviewer_profile", "")),
            status=str(raw.get("status", "REVIEW_UNKNOWN")).upper(),
            agreement=str(raw.get("agreement", "PARTIAL")).upper(),
            final_verdict=str(raw.get("final_verdict", "UNKNOWN")).upper(),
            confidence=_clamp_confidence(raw.get("confidence", 0.0)),
            reason=str(raw.get("reason", "")),
            patch_required=bool(raw.get("patch_required", False)),
            analyst_results=[item for item in raw.get("analyst_results", []) if isinstance(item, dict)],
            reviewer_result=raw.get("reviewer_result") if isinstance(raw.get("reviewer_result"), dict) else {},
            evidence_refs=[str(item) for item in raw.get("evidence_refs", []) if str(item).strip()],
            provider_requests=int(raw.get("provider_requests", 0) or 0),
            logical_calls=int(raw.get("logical_calls", 0) or 0),
            provider_retries=int(raw.get("provider_retries", 0) or 0),
            json_repairs=int(raw.get("json_repairs", 0) or 0),
            stage_regenerations=int(raw.get("stage_regenerations", 0) or 0),
            input_tokens=int(raw.get("input_tokens", 0) or 0),
            output_tokens=int(raw.get("output_tokens", 0) or 0),
            review_429_count=int(raw.get("review_429_count", 0) or 0),
            review_503_count=int(raw.get("review_503_count", 0) or 0),
            review_timeout_count=int(raw.get("review_timeout_count", 0) or 0),
            provider_health=_provider_health_from_raw(raw),
        )


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(1.0, confidence))


def _provider_health_from_raw(raw: dict[str, Any]) -> dict[str, int] | None:
    provider_health = raw.get("provider_health")
    if isinstance(provider_health, dict):
        return {
            "429": int(provider_health.get("429") or 0),
            "503": int(provider_health.get("503") or 0),
            "timeout": int(provider_health.get("timeout") or 0),
            "malformed_json": int(provider_health.get("malformed_json") or 0),
            "truncated_response": int(provider_health.get("truncated_response") or 0),
            "other_error": int(provider_health.get("other_error") or 0),
        }
    if any(key in raw for key in ("review_429_count", "review_503_count", "review_timeout_count")):
        return {
            "429": int(raw.get("review_429_count", 0) or 0),
            "503": int(raw.get("review_503_count", 0) or 0),
            "timeout": int(raw.get("review_timeout_count", 0) or 0),
            "malformed_json": 0,
            "truncated_response": 0,
            "other_error": 0,
        }
    return None
