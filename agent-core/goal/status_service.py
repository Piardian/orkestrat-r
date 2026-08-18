from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .service import GoalService
from .audit_report import is_read_only_audit


STAGE_MAP = {
    "CREATED": "INTAKE",
    "PLANNING": "PLANNING",
    "PLANNING_FAILED": "PLANNING",
    "PLANNED": "PLAN_REVIEW",
    "REVIEWING": "PLAN_REVIEW",
    "REVISION_REQUIRED": "PLAN_REVIEW",
    "REVIEW_UNKNOWN": "PLAN_REVIEW",
    "REVIEW_FAILED": "PLAN_REVIEW",
    "APPROVED": "COMPLEXITY",
    "COMPLEXITY_ASSESSING": "COMPLEXITY",
    "COMPLEXITY_FAILED": "COMPLEXITY",
    "READY_FOR_OPENHANDS": "BUILD",
    "BUILDING": "BUILD",
    "BUILD_FAILED": "BUILD",
    "BUILDER_BLOCKED": "BUILD",
    "BUILDER_POLICY_VIOLATION": "BUILD",
    "BUILT_PENDING_REVIEW": "FINAL_REVIEW",
    "BUILD_REVIEWING": "FINAL_REVIEW",
    "BUILD_REJECTED": "FINAL_REVIEW",
    "BUILD_REVIEW_FAILED": "FINAL_REVIEW",
    "BUILD_REVIEW_UNKNOWN": "FINAL_REVIEW",
    "READY_TO_APPLY": "APPLY",
    "APPLYING": "APPLY",
    "APPLY_FAILED": "APPLY",
    "POST_APPLY_VERIFICATION_FAILED": "APPLY",
    "COMPLETED": "COMPLETED",
    "WAITING_CODEX": "BUILD",
    "CODEX_REQUIRED": "BUILD",
    "CODEX_RESPONSE_RECEIVED": "BUILD",
    "CODEX_PATCH_STAGING": "BUILD",
}


CURRENT_TEXT = {
    "CREATED": "Waiting for planning",
    "PLANNING": "Planner is running (in-flight)",
    "PLANNING_FAILED": "Planning failed",
    "PLANNED": "Waiting for review",
    "REVIEWING": "Review is running (in-flight)",
    "REVISION_REQUIRED": "Plan revision required",
    "REVIEW_UNKNOWN": "Review inconclusive",
    "REVIEW_FAILED": "Plan review failed",
    "APPROVED": "Waiting for complexity gate",
    "COMPLEXITY_ASSESSING": "Complexity gate is running (in-flight)",
    "COMPLEXITY_FAILED": "Complexity assessment failed",
    "READY_FOR_OPENHANDS": "Waiting for OpenHands execution",
    "BUILDING": "OpenHands build is running (in-flight)",
    "BUILD_FAILED": "Build failed",
    "BUILDER_BLOCKED": "Builder blocked",
    "BUILDER_POLICY_VIOLATION": "Builder policy violation",
    "BUILT_PENDING_REVIEW": "Waiting for final build review",
    "BUILD_REVIEWING": "Final review is running (in-flight)",
    "BUILD_REJECTED": "Build rejected by review",
    "BUILD_REVIEW_FAILED": "Final review failed",
    "BUILD_REVIEW_UNKNOWN": "Final review inconclusive",
    "READY_TO_APPLY": "Waiting for explicit apply approval",
    "APPLYING": "Applying to target repo (in-flight)",
    "APPLY_FAILED": "Apply failed",
    "POST_APPLY_VERIFICATION_FAILED": "Post-apply verification failed",
    "COMPLETED": "Done",
    "WAITING_CODEX": "Waiting for manual Codex response",
    "CODEX_REQUIRED": "Codex manual workflow required",
    "CODEX_RESPONSE_RECEIVED": "Codex response received",
    "CODEX_PATCH_STAGING": "Codex patch is being staged",
}

NEXT_ACTION = {
    "CREATED": "python run_goal.py --goal-id GOAL-...",
    "PLANNING": "python run_goal.py --goal-id GOAL-...",
    "PLANNING_FAILED": "python run_goal.py --goal-id GOAL-...",
    "PLANNED": "python review_goal.py --goal-id GOAL-...",
    "REVIEWING": "python review_goal.py --goal-id GOAL-...",
    "REVIEW_FAILED": "python review_goal.py --goal-id GOAL-...",
    "REVISION_REQUIRED": "python run_goal.py --goal-id GOAL-...",
    "REVIEW_UNKNOWN": "python review_goal.py --goal-id GOAL-...",
    "APPROVED": "python assess_goal.py --goal-id GOAL-...",
    "COMPLEXITY_ASSESSING": "python assess_goal.py --goal-id GOAL-...",
    "COMPLEXITY_FAILED": "python assess_goal.py --goal-id GOAL-...",
    "READY_FOR_OPENHANDS": "python run_builder.py --goal-id GOAL-... --execute",
    "BUILDING": "python run_builder.py --goal-id GOAL-... --execute",
    "BUILD_FAILED": "python run_builder.py --goal-id GOAL-... --execute",
    "BUILDER_BLOCKED": "python run_builder.py --goal-id GOAL-... --execute",
    "BUILDER_POLICY_VIOLATION": "python run_builder.py --goal-id GOAL-... --execute",
    "BUILT_PENDING_REVIEW": "python finalize_goal.py review --goal-id GOAL-...",
    "BUILD_REVIEWING": "python finalize_goal.py review --goal-id GOAL-...",
    "BUILD_REJECTED": "python run_builder.py --goal-id GOAL-... --execute",
    "BUILD_REVIEW_FAILED": "python finalize_goal.py review --goal-id GOAL-...",
    "BUILD_REVIEW_UNKNOWN": "python finalize_goal.py review --goal-id GOAL-...",
    "WAITING_CODEX": "python codex_goal.py submit --goal-id GOAL-...",
    "READY_TO_APPLY": "python finalize_goal.py apply --goal-id GOAL-... --apply",
    "APPLYING": "python finalize_goal.py apply --goal-id GOAL-... --apply",
    "APPLY_FAILED": "python finalize_goal.py apply --goal-id GOAL-... --apply",
    "POST_APPLY_VERIFICATION_FAILED": "python finalize_goal.py apply --goal-id GOAL-... --apply",
}


@dataclass(frozen=True)
class GoalStatusSnapshot:
    goal_id: str
    goal: str
    repo: str
    state: str
    stage: str
    current: str
    completed: list[str]
    remaining: list[str]
    next_action: str | None
    updated_at: str
    status_history: list[dict[str, Any]]
    goal_type: str = "CODE_MODIFICATION"


class GoalStatusService:
    def __init__(self, service: GoalService | None = None) -> None:
        self.service = service or GoalService()

    def list_goals(self, status: str | None = None, limit: int = 20) -> list[GoalStatusSnapshot]:
        records = [self.service.read_goal(goal_id) for goal_id in self.service.store.list_goal_ids()]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        if status:
            want = status.strip().upper()
            records = [item for item in records if item.status.strip().upper() == want]
        return [self.snapshot(item.goal_id) for item in records[:limit]]

    def snapshot(self, goal_id: str) -> GoalStatusSnapshot:
        record = self.service.read_goal(goal_id)
        history = self.service.store.read_jsonl(goal_id, "history.jsonl")
        goal_type = getattr(record, "goal_type", "READ_ONLY_AUDIT" if is_read_only_audit(record.goal) else "CODE_MODIFICATION")
        completed, remaining = self._stage_lists(record.status, goal_type)

        if goal_type == "READ_ONLY_AUDIT" and record.status == "COMPLETED":
            current = "Done (Read-only audit report generated; no code modifications required)"
            next_action = f"No action required (Audit report: runtime/goals/{record.goal_id}/audit_report.md)"
        else:
            current = CURRENT_TEXT.get(record.status.strip().upper(), record.status)
            next_action = NEXT_ACTION.get(record.status.strip().upper())

        return GoalStatusSnapshot(
            goal_id=record.goal_id,
            goal=record.goal,
            repo=record.repo,
            state=record.status,
            stage=STAGE_MAP.get(record.status.strip().upper(), "UNKNOWN"),
            current=current,
            completed=completed,
            remaining=remaining,
            next_action=next_action,
            updated_at=record.updated_at,
            status_history=history,
            goal_type=goal_type,
        )

    def history(self, goal_id: str) -> list[dict[str, Any]]:
        return self.service.store.read_jsonl(goal_id, "history.jsonl")

    def _stage_lists(self, state: str, goal_type: str = "CODE_MODIFICATION") -> tuple[list[str], list[str]]:
        if goal_type == "READ_ONLY_AUDIT":
            order = ["INTAKE", "PLANNING", "AUDITING / REVIEW", "AUDIT_REPORT", "COMPLETED"]
            state_upper = state.strip().upper()
            if state_upper == "CREATED":
                return ["INTAKE"], ["PLANNING", "AUDITING / REVIEW", "AUDIT_REPORT", "COMPLETED"]
            if state_upper in {"PLANNING", "PLANNED"}:
                return ["INTAKE", "PLANNING"], ["AUDITING / REVIEW", "AUDIT_REPORT", "COMPLETED"]
            if state_upper in {"REVIEWING", "APPROVED"}:
                return ["INTAKE", "PLANNING", "AUDITING / REVIEW"], ["AUDIT_REPORT", "COMPLETED"]
            if state_upper == "COMPLETED":
                return ["INTAKE", "PLANNING", "AUDITING / REVIEW", "AUDIT_REPORT", "COMPLETED"], []
            return ["INTAKE"], ["PLANNING", "AUDITING / REVIEW", "AUDIT_REPORT", "COMPLETED"]

        order = ["INTAKE", "PLANNING", "PLAN_REVIEW", "COMPLEXITY", "BUILD", "FINAL_REVIEW", "APPLY", "COMPLETED"]
        current = STAGE_MAP.get(state.strip().upper(), "UNKNOWN")
        if current == "UNKNOWN":
            return [], []
        idx = order.index(current)
        return order[: idx + 1], order[idx + 1 :]
