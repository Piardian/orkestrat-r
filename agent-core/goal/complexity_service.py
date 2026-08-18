from __future__ import annotations

from pathlib import Path

from .complexity import ComplexityAssessment, ComplexityAssessor
from .metrics_service import GoalMetricsService
from .model import GoalRecord
from .service import GoalService


class GoalComplexityService:
    def __init__(self, service: GoalService | None = None, assessor: ComplexityAssessor | None = None) -> None:
        self.service = service or GoalService()
        self.assessor = assessor or ComplexityAssessor()

    def assess_goal(self, goal_id: str, force: bool = False) -> tuple[GoalRecord, ComplexityAssessment]:
        record = self.service.read_goal(goal_id)
        if record.status.strip().upper() != "APPROVED":
            raise ValueError("Goal must be APPROVED before complexity assessment.")

        goal_dir = self.service.store.goal_dir(goal_id)
        complexity_path = goal_dir / "complexity.json"
        if complexity_path.exists() and not force:
            raise FileExistsError(complexity_path)

        record = self.service.update_status(record, "COMPLEXITY_ASSESSING", phase="complexity-assessing", note="complexity gate started")
        try:
            assessment = self.assessor.assess(goal_dir)
            next_state = "READY_FOR_OPENHANDS" if assessment.severity in {"EASY", "MEDIUM"} else "CODEX_REQUIRED"
            final_record = self.service.update_status(record, next_state, phase="complexity-assessed", note=f"severity={assessment.severity};executor={assessment.recommended_executor}")
            self.service.store.save_complexity_bundle(final_record, assessment)
            GoalMetricsService(self.service).refresh_goal(final_record.goal_id)
            return final_record, assessment
        except Exception as exc:
            failed = self.service.update_status(record, "COMPLEXITY_FAILED", phase="complexity-failed", note=str(exc))
            self.service.store.save_complexity_bundle(
                failed,
                ComplexityAssessment(
                    version=1,
                    goal_id=goal_id,
                    score=0,
                    severity="EASY",
                    recommended_executor="openhands",
                    factors=[],
                    hard_overrides=[],
                    candidate_file_count=0,
                    module_count=0,
                    review_risk_count=0,
                    llm_calls=0,
                ),
            )
            GoalMetricsService(self.service).refresh_goal(failed.goal_id)
            raise
