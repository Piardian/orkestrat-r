from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builder_service import GoalBuilderService
from .codex_service import GoalCodexService
from .complexity_service import GoalComplexityService
from .finalize import FinalReviewService
from .model import GoalRecord
from .planner import GoalPlanner
from .review_service import GoalReviewService
from .service import GoalService
from .status_service import GoalStatusService


@dataclass(frozen=True)
class ResumeResult:
    goal_id: str
    state: str
    action: str
    executed: bool
    message: str
    lock_acquired: bool


class GoalResumeService:
    def __init__(self, service: GoalService | None = None, runtime_root: str | Path = "runtime") -> None:
        self.service = service or GoalService()
        self.runtime_root = Path(runtime_root)
        self.locks_root = self.runtime_root / "locks"
        self.locks_root.mkdir(parents=True, exist_ok=True)

    def resume(self, goal_id: str, execute: bool = False) -> ResumeResult:
        record = self.service.read_goal(goal_id)
        snapshot = GoalStatusService(self.service).snapshot(goal_id)
        state = record.status.strip().upper()
        action = snapshot.next_action or "No action required"

        if state == "COMPLETED":
            return ResumeResult(goal_id, state, action, False, "Goal is already COMPLETED. No action required.", False)

        if state in {"WAITING_CODEX", "READY_TO_APPLY", "APPLYING", "APPLY_FAILED", "POST_APPLY_VERIFICATION_FAILED"}:
            return ResumeResult(goal_id, state, action, False, self._manual_message(state, goal_id), False)

        if not execute:
            return ResumeResult(goal_id, state, action, False, "Dry-run only. Pass --execute to run the next pipeline stage.", False)

        with self._acquire_lock(goal_id) as locked:
            if not locked:
                return ResumeResult(goal_id, state, action, False, "Resume blocked: another active process holds the lock.", False)
            return self._dispatch(record)

    def _manual_message(self, state: str, goal_id: str) -> str:
        if state == "WAITING_CODEX":
            return f"MANUAL ACTION REQUIRED: Codex response is waiting. Use python codex_goal.py submit --goal-id {goal_id}"
        if state in {"READY_TO_APPLY", "APPLYING", "APPLY_FAILED", "POST_APPLY_VERIFICATION_FAILED"}:
            return f"USER APPROVAL REQUIRED: Goal is ready to apply. Use python finalize_goal.py apply --goal-id {goal_id} --apply"
        return f"Manual recovery action required for state {state}."

    def _dispatch(self, record: GoalRecord) -> ResumeResult:
        state = record.status.strip().upper()
        if state in {"CREATED", "PLANNING", "PLANNING_FAILED"}:
            if state in {"PLANNING", "PLANNING_FAILED"}:
                record = self.service.update_status(record, "CREATED", phase="intake", note="retry planning")
            record, *_ = GoalPlanner(service=self.service).plan_goal(record.goal_id)
            return ResumeResult(record.goal_id, record.status, "planner", True, "Planner executed.", True)

        if state in {"PLANNED", "REVIEWING", "REVIEW_FAILED", "REVISION_REQUIRED", "REVIEW_UNKNOWN"}:
            if state in {"REVIEWING", "REVIEW_FAILED", "REVISION_REQUIRED", "REVIEW_UNKNOWN"}:
                record = self.service.update_status(record, "PLANNED", phase="planned", note="retry review")
            record, *_ = GoalReviewService(service=self.service).review_goal(record.goal_id)
            return ResumeResult(record.goal_id, record.status, "review", True, "Review executed.", True)

        if state in {"APPROVED", "COMPLEXITY_ASSESSING", "COMPLEXITY_FAILED"}:
            if state in {"COMPLEXITY_ASSESSING", "COMPLEXITY_FAILED"}:
                record = self.service.update_status(record, "APPROVED", phase="approved", note="retry complexity")
            record, _ = GoalComplexityService(service=self.service).assess_goal(record.goal_id, force=True)
            return ResumeResult(record.goal_id, record.status, "complexity", True, "Complexity executed.", True)

        if state in {"READY_FOR_OPENHANDS", "BUILDING", "BUILD_FAILED", "BUILDER_BLOCKED", "BUILDER_POLICY_VIOLATION", "BUILD_REJECTED"}:
            if state in {"BUILDING", "BUILD_FAILED", "BUILDER_BLOCKED", "BUILDER_POLICY_VIOLATION", "BUILD_REJECTED"}:
                record = self.service.update_status(record, "READY_FOR_OPENHANDS", phase="complexity-assessed", note="retry build")
            record, _, _ = GoalBuilderService(service=self.service).execute(record.goal_id)
            return ResumeResult(record.goal_id, record.status, "builder", True, "OpenHands builder executed.", True)

        if state in {"BUILT_PENDING_REVIEW", "BUILD_REVIEWING", "BUILD_REVIEW_FAILED", "BUILD_REVIEW_UNKNOWN"}:
            if state in {"BUILD_REVIEWING", "BUILD_REVIEW_FAILED", "BUILD_REVIEW_UNKNOWN"}:
                record = self.service.update_status(record, "BUILT_PENDING_REVIEW", phase="built-pending-review", note="retry final review")
            record, _ = FinalReviewService(service=self.service, runtime_root=self.runtime_root).review(record.goal_id)
            return ResumeResult(record.goal_id, record.status, "final-review", True, "Final review executed.", True)

        if state == "CODEX_REQUIRED":
            record, _, _ = GoalCodexService(service=self.service, runtime_root=self.runtime_root).prepare(record.goal_id)
            return ResumeResult(record.goal_id, record.status, "codex-prepare", True, "Codex prompt prepared.", True)

        return ResumeResult(record.goal_id, record.status, "noop", False, "No resume action configured for this state.", True)

    def _acquire_lock(self, goal_id: str):
        lock_path = self.locks_root / f"{goal_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if lock_path.exists():
            try:
                content = lock_path.read_text(encoding="utf-8").strip()
                parts = content.split()
                if parts:
                    pid = int(parts[0])
                    lock_time = int(parts[1]) if len(parts) > 1 else 0
                    now = int(time.time())
                    # If process is dead or lock is stale (> 600s), unlink stale lock
                    if not _is_pid_alive(pid) or (now - lock_time > 600):
                        lock_path.unlink()
            except Exception:
                try:
                    lock_path.unlink()
                except Exception:
                    pass

        class _Ctx:
            def __enter__(self_nonlocal):
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(f"{os.getpid()} {int(time.time())}")
                    return True
                except FileExistsError:
                    return False

            def __exit__(self_nonlocal, exc_type, exc, tb):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

        return _Ctx()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False
