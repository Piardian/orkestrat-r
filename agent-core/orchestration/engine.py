from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from goal import (
    FinalReviewService,
    GoalComplexityService,
    GoalPlanner,
    GoalReviewService,
    GoalService,
)
from llm.router import LLMRouter
from routing import resolve_commander_profile


@dataclass(frozen=True)
class PipelineRequest:
    repo: str | None = None
    task: str | None = None
    goal_id: str | None = None
    runtime_dir: str = "runtime/goals"
    execution_mode: str | None = None
    commander_profile: str | None = None
    openhands_python: str | None = None
    auto_apply: bool = False


@dataclass
class PipelineRunResult:
    goal_id: str
    state: str
    stages: list[dict[str, str]] = field(default_factory=list)
    auto_apply: bool = False
    applied: bool = False
    next_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PipelineStageError(RuntimeError):
    def __init__(self, stage: str, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.returncode = returncode


class GoalPipelineEngine:
    """Thin orchestration layer over the existing deterministic goal state machine.

    The existing Goal* services remain the source of truth. CrewAI may call these
    methods, but it does not own authorization, file scope, verification, or final
    apply decisions.
    """

    def __init__(self, request: PipelineRequest, service: GoalService | None = None) -> None:
        self.request = request
        self.core_dir = Path(__file__).resolve().parents[1]
        runtime_dir = Path(request.runtime_dir)
        if not runtime_dir.is_absolute():
            runtime_dir = (self.core_dir / runtime_dir).resolve()
        self.runtime_dir = runtime_dir
        self.runtime_root = self.runtime_dir.parent if self.runtime_dir.name == "goals" else self.core_dir / "runtime"
        self.service = service or GoalService(base_dir=self.runtime_dir)
        self._stages: list[dict[str, str]] = []
        self._applied = False

    def create_or_resume(self) -> str:
        if self.request.goal_id:
            record = self.service.read_goal(self.request.goal_id)
            self._event("intake", "resume", record.status)
            return record.goal_id
        if not self.request.repo or not self.request.task:
            raise PipelineStageError("intake", "repo and task are required when goal_id is not supplied")
        record = self.service.create_goal(self.request.task, Path(self.request.repo))
        self._event("intake", "created", record.status)
        return record.goal_id

    def plan(self, goal_id: str) -> str:
        state = self._state(goal_id)
        if state in {"CREATED", "PLANNING_FAILED"}:
            router = LLMRouter()
            planner = GoalPlanner(service=self.service, router=router, execution_mode=self.request.execution_mode)
            profile = resolve_commander_profile(self.request.commander_profile)
            record, *_ = planner.plan_goal(goal_id, commander_profile=profile)
            self._event("planning", "executed", record.status)
            return record.status
        self._event("planning", "skipped", state)
        return state

    def review_plan(self, goal_id: str) -> str:
        state = self._state(goal_id)
        if state == "PLANNED":
            review = GoalReviewService(
                service=self.service,
                router=LLMRouter(),
                execution_mode=self.request.execution_mode,
            )
            record, *_ = review.review_goal(goal_id)
            self._event("plan-review", "executed", record.status)
            return record.status
        self._event("plan-review", "skipped", state)
        return state

    def assess_complexity(self, goal_id: str) -> str:
        state = self._state(goal_id)
        if state == "APPROVED":
            record, _ = GoalComplexityService(service=self.service).assess_goal(goal_id)
            self._event("complexity", "executed", record.status)
            return record.status
        self._event("complexity", "skipped", state)
        return state

    def build(self, goal_id: str) -> str:
        state = self._state(goal_id)
        if state != "READY_FOR_OPENHANDS":
            self._event("openhands-build", "skipped", state)
            return state

        python_exe = resolve_openhands_python(self.request.openhands_python, self.core_dir)
        command = [
            python_exe,
            str(self.core_dir / "run_builder.py"),
            "--goal-id",
            goal_id,
            "--runtime-dir",
            str(self.runtime_dir),
            "--execute",
        ]
        if self.request.execution_mode:
            command.extend(["--mode", self.request.execution_mode])

        timeout_seconds = max(60.0, float(os.getenv("AGENT_ARMY_BUILDER_TIMEOUT_SECONDS", "3600")))
        try:
            proc = subprocess.run(
                command,
                cwd=str(self.core_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            try:
                record = self.service.read_goal(goal_id)
                if record.status.strip().upper() == "BUILDING":
                    self.service.update_status(
                        record,
                        "BUILD_FAILED",
                        phase="build-failed",
                        note=f"builder process timed out after {timeout_seconds:.0f}s",
                    )
            except Exception:
                pass
            output = (exc.stderr or exc.stdout or "") if isinstance(exc.stderr or exc.stdout or "", str) else ""
            raise PipelineStageError(
                "openhands-build",
                _clip(output) or f"OpenHands builder timed out after {timeout_seconds:.0f}s",
                returncode=124,
            ) from exc

        state = self._state(goal_id)
        if proc.returncode != 0 and state not in {
            "BUILD_FAILED",
            "BUILDER_BLOCKED",
            "BUILDER_POLICY_VIOLATION",
        }:
            detail = _clip(proc.stderr.strip() or proc.stdout.strip())
            raise PipelineStageError("openhands-build", detail or "OpenHands builder failed", returncode=proc.returncode)
        self._event("openhands-build", "executed" if proc.returncode == 0 else "failed", state)
        return state

    def final_review(self, goal_id: str) -> str:
        state = self._state(goal_id)
        if state == "BUILT_PENDING_REVIEW":
            final = FinalReviewService(service=self.service, runtime_root=self.runtime_root)
            record, _ = final.review(goal_id)
            self._event("final-review", "executed", record.status)
            return record.status
        self._event("final-review", "skipped", state)
        return state

    def apply(self, goal_id: str) -> str:
        state = self._state(goal_id)
        if not self.request.auto_apply:
            self._event("apply", "manual-gate", state)
            return state
        if state in {"READY_TO_APPLY", "APPLYING"}:
            final = FinalReviewService(service=self.service, runtime_root=self.runtime_root)
            record, result = final.apply(goal_id, explicit_apply=True)
            self._applied = str(result.get("status", "")).upper() == "PASS" and record.status == "COMPLETED"
            self._event("apply", "resumed" if state == "APPLYING" else "executed", record.status)
            return record.status
        self._event("apply", "skipped", state)
        return state

    def finish(self, goal_id: str) -> PipelineRunResult:
        state = self._state(goal_id)
        return PipelineRunResult(
            goal_id=goal_id,
            state=state,
            stages=list(self._stages),
            auto_apply=self.request.auto_apply,
            applied=self._applied,
            next_action=_next_action(state),
        )

    def run_native(self) -> PipelineRunResult:
        goal_id = self.create_or_resume()
        self.plan(goal_id)
        self.review_plan(goal_id)
        self.assess_complexity(goal_id)
        self.build(goal_id)
        self.final_review(goal_id)
        self.apply(goal_id)
        return self.finish(goal_id)

    def _state(self, goal_id: str) -> str:
        return self.service.read_goal(goal_id).status.strip().upper()

    def _event(self, stage: str, action: str, state: str) -> None:
        self._stages.append({"stage": stage, "action": action, "state": state})


def resolve_openhands_python(explicit: str | None, core_dir: Path) -> str:
    candidates = [
        explicit,
        os.getenv("OPENHANDS_PYTHON"),
        str(core_dir / ".venv-openhands" / "Scripts" / "python.exe"),
        str(core_dir / ".venv-openhands" / "bin" / "python"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    return sys.executable


def _next_action(state: str) -> str | None:
    mapping = {
        "READY_TO_APPLY": "Explicit user approval is required before applying the patch.",
        "APPLYING": "Resume the persisted transactional apply; do not start a second apply.",
        "CODEX_REQUIRED": "Use the existing Codex/manual path for this high-complexity goal.",
        "BUILD_FAILED": "Inspect build.json/verification.json, then retry the OpenHands build.",
        "BUILDER_BLOCKED": "Resolve the builder preflight block, then retry.",
        "BUILDER_POLICY_VIOLATION": "Review unauthorized changes before retrying.",
        "BUILD_REJECTED": "Review final-review findings and rebuild.",
        "POST_APPLY_VERIFICATION_FAILED": "The verified promotion was rolled back; inspect verification output before retrying.",
        "COMPLETED": None,
    }
    return mapping.get(state, f"Continue from state {state} using the existing goal state machine.")


def _clip(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "..."
