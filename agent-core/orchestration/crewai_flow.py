from __future__ import annotations

from typing import Any

from .engine import GoalPipelineEngine, PipelineRunResult


class CrewAIUnavailableError(RuntimeError):
    pass


try:
    from crewai.flow.flow import Flow, listen, start
except Exception as exc:  # pragma: no cover - depends on optional package
    _CREWAI_IMPORT_ERROR = exc
    Flow = None  # type: ignore[assignment]
    listen = None  # type: ignore[assignment]
    start = None  # type: ignore[assignment]
else:
    _CREWAI_IMPORT_ERROR = None


if Flow is not None:

    class CrewAIGoalFlow(Flow):
        """CrewAI Flow wrapper around the existing goal state machine.

        CrewAI owns stage sequencing only. The project's existing services own
        state transitions, scope checks, verification and the final apply gate.
        """

        def __init__(self, engine: GoalPipelineEngine) -> None:
            super().__init__()
            self.engine = engine

        @start()
        def intake(self) -> str:
            return self.engine.create_or_resume()

        @listen(intake)
        def planning(self, goal_id: str) -> str:
            self.engine.plan(goal_id)
            return goal_id

        @listen(planning)
        def plan_review(self, goal_id: str) -> str:
            self.engine.review_plan(goal_id)
            return goal_id

        @listen(plan_review)
        def complexity(self, goal_id: str) -> str:
            self.engine.assess_complexity(goal_id)
            return goal_id

        @listen(complexity)
        def openhands_build(self, goal_id: str) -> str:
            self.engine.build(goal_id)
            return goal_id

        @listen(openhands_build)
        def final_review(self, goal_id: str) -> str:
            self.engine.final_review(goal_id)
            return goal_id

        @listen(final_review)
        def apply_gate(self, goal_id: str) -> str:
            self.engine.apply(goal_id)
            return goal_id

        @listen(apply_gate)
        def finish(self, goal_id: str) -> dict[str, Any]:
            return self.engine.finish(goal_id).to_dict()


def run_crewai_flow(engine: GoalPipelineEngine) -> PipelineRunResult:
    if Flow is None:
        raise CrewAIUnavailableError(
            "CrewAI is not installed. Install agent-core/requirements-crewai.txt in the orchestration environment."
        ) from _CREWAI_IMPORT_ERROR
    result = CrewAIGoalFlow(engine).kickoff()
    if isinstance(result, PipelineRunResult):
        return result
    if isinstance(result, dict):
        return PipelineRunResult(**result)
    return engine.finish(engine.request.goal_id or _last_goal_id(engine))


def _last_goal_id(engine: GoalPipelineEngine) -> str:
    ids = engine.service.store.list_goal_ids()
    if not ids:
        raise RuntimeError("CrewAI flow completed without a goal id")
    return ids[-1]
