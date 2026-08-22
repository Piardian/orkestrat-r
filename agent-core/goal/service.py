from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import time

from .model import GoalRecord
from .validator import validate_goal_text, validate_repo_path
from .store import GoalStore, build_goal_store
from .audit_report import is_read_only_audit


GOAL_ID_PATTERN = re.compile(r"^GOAL-\d{8}-\d{4}$")
ALLOWED_STATUS_TRANSITIONS = {
    "CREATED": {"PLANNING", "PLANNING_FAILED"},
    "PLANNING": {"PLANNED", "PLANNING_FAILED", "PLANNING", "CREATED"},
    "PLANNING_FAILED": {"PLANNING", "CREATED"},
    "PLANNED": {"REVIEWING", "PLANNING"},
    "REVIEWING": {"APPROVED", "COMPLETED", "REVISION_REQUIRED", "REVIEW_UNKNOWN", "REVIEW_FAILED", "REVIEWING", "PLANNED"},
    "APPROVED": {"COMPLEXITY_ASSESSING", "COMPLETED", "REVIEWING"},
    "REVISION_REQUIRED": {"PLANNING", "REVIEWING", "APPROVED"},
    "REVIEW_UNKNOWN": {"REVIEWING", "PLANNING", "APPROVED"},
    "REVIEW_FAILED": {"REVIEWING", "PLANNING", "PLANNED", "APPROVED"},
    "COMPLEXITY_ASSESSING": {"READY_FOR_OPENHANDS", "CODEX_REQUIRED", "COMPLEXITY_FAILED", "COMPLEXITY_ASSESSING", "APPROVED"},
    "COMPLEXITY_FAILED": {"COMPLEXITY_ASSESSING", "APPROVED"},
    "CODEX_REQUIRED": {"WAITING_CODEX", "READY_FOR_OPENHANDS", "CODEX_REQUIRED"},
    "WAITING_CODEX": {"CODEX_RESPONSE_RECEIVED", "CODEX_RESPONSE_INVALID", "CODEX_POLICY_VIOLATION", "CODEX_WORKSPACE_BLOCKED", "WAITING_CODEX"},
    "CODEX_RESPONSE_RECEIVED": {"CODEX_PATCH_STAGING", "CODEX_RESPONSE_INVALID", "CODEX_POLICY_VIOLATION", "CODEX_PATCH_FAILED", "CODEX_WORKSPACE_BLOCKED"},
    "CODEX_PATCH_STAGING": {"BUILT_PENDING_REVIEW", "CODEX_VERIFICATION_FAILED", "CODEX_PATCH_FAILED", "CODEX_POLICY_VIOLATION"},
    "CODEX_RESPONSE_INVALID": {"WAITING_CODEX"},
    "CODEX_POLICY_VIOLATION": {"WAITING_CODEX"},
    "CODEX_PATCH_FAILED": {"WAITING_CODEX"},
    "CODEX_VERIFICATION_FAILED": {"WAITING_CODEX"},
    "CODEX_WORKSPACE_BLOCKED": {"WAITING_CODEX", "CODEX_REQUIRED"},
    "READY_FOR_OPENHANDS": {"BUILDING", "BUILDER_BLOCKED", "BUILD_FAILED", "BUILDER_POLICY_VIOLATION", "READY_FOR_OPENHANDS"},
    "BUILDING": {"BUILT_PENDING_REVIEW", "BUILD_FAILED", "BUILDER_BLOCKED", "BUILDER_POLICY_VIOLATION", "BUILDING", "READY_FOR_OPENHANDS"},
    "BUILDER_BLOCKED": {"READY_FOR_OPENHANDS", "BUILDING"},
    "BUILD_FAILED": {"READY_FOR_OPENHANDS", "BUILDING"},
    "BUILDER_POLICY_VIOLATION": {"READY_FOR_OPENHANDS", "BUILDING"},
    "BUILT_PENDING_REVIEW": {"BUILD_REVIEWING", "BUILD_REJECTED", "BUILD_FAILED"},
    "BUILD_REVIEWING": {"READY_TO_APPLY", "BUILD_REJECTED", "BUILD_REVIEW_UNKNOWN", "BUILD_REVIEW_FAILED", "BUILD_REVIEWING", "BUILT_PENDING_REVIEW"},
    "BUILD_REJECTED": {"BUILT_PENDING_REVIEW", "BUILD_REVIEWING", "READY_FOR_OPENHANDS"},
    "BUILD_REVIEW_UNKNOWN": {"BUILD_REVIEWING", "BUILT_PENDING_REVIEW"},
    "BUILD_REVIEW_FAILED": {"BUILD_REVIEWING", "BUILT_PENDING_REVIEW"},
    "READY_TO_APPLY": {"APPLYING", "APPLY_FAILED"},
    "APPLYING": {"COMPLETED", "APPLY_FAILED", "POST_APPLY_VERIFICATION_FAILED", "APPLYING", "READY_TO_APPLY"},
    "APPLY_FAILED": {"READY_TO_APPLY", "APPLYING"},
    "POST_APPLY_VERIFICATION_FAILED": {"READY_TO_APPLY", "APPLYING"},
    "COMPLETED": set(),
    "INVALID": set(),
}


class GoalService:
    def __init__(self, store: GoalStore | None = None, base_dir: str | Path | None = None) -> None:
        if store is not None:
            self.store = store
        else:
            self.store = build_goal_store(base_dir or "runtime/goals")

    def create_goal(self, goal: str, repo: str | Path, idempotency_key: str | None = None) -> GoalRecord:
        normalized_goal = validate_goal_text(goal)
        repo_path = validate_repo_path(repo)
        request_key = (idempotency_key or "").strip()
        goal_type = "READ_ONLY_AUDIT" if is_read_only_audit(normalized_goal) else "CODE_MODIFICATION"

        if request_key:
            existing = self.store.lookup_idempotency_key(request_key)
            if existing:
                return self._load_or_recover_claimed_goal(existing, normalized_goal, repo_path, goal_type)

        goal_id = self._next_goal_id()
        now = self._now()
        record = GoalRecord(
            goal_id=goal_id,
            goal=normalized_goal,
            repo=str(repo_path),
            status="CREATED",
            created_at=now,
            updated_at=now,
            phase="intake",
            utc_timestamp=True,
            goal_type=goal_type,
            notes=[],
        )

        if request_key:
            claimed = self.store.claim_idempotency_key(request_key, goal_id)
            if claimed != goal_id:
                self._release_unused_reservation(goal_id)
                return self._load_or_recover_claimed_goal(claimed, normalized_goal, repo_path, goal_type)

        self.store.save(record)
        return record

    def _load_or_recover_claimed_goal(
        self,
        goal_id: str,
        normalized_goal: str,
        repo_path: Path,
        goal_type: str,
    ) -> GoalRecord:
        try:
            return self._load_claimed_goal(goal_id)
        except (FileNotFoundError, OSError):
            # The prior worker may have committed the idempotency claim and died
            # before persisting the CREATED row. Recover the same goal id; never
            # allocate a second identity for this request.
            now = self._now()
            recovered = GoalRecord(
                goal_id=goal_id,
                goal=normalized_goal,
                repo=str(repo_path),
                status="CREATED",
                created_at=now,
                updated_at=now,
                phase="intake-recovered",
                utc_timestamp=True,
                goal_type=goal_type,
                notes=["Recovered idempotent intake after incomplete persistence."],
            )
            self.store.goal_dir(goal_id).mkdir(parents=True, exist_ok=True)
            self.store.save(recovered)
            return recovered

    def _load_claimed_goal(self, goal_id: str) -> GoalRecord:
        # A competing worker can claim the request key a few milliseconds before
        # its state row/file becomes visible. Bound the wait rather than creating
        # a duplicate goal.
        last_error: Exception | None = None
        for _ in range(20):
            try:
                return self.read_goal(goal_id)
            except (FileNotFoundError, OSError) as exc:
                last_error = exc
                time.sleep(0.05)
        if last_error:
            raise last_error
        return self.read_goal(goal_id)

    def _release_unused_reservation(self, goal_id: str) -> None:
        try:
            directory = self.store.goal_dir(goal_id)
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass

    def read_goal(self, goal_id: str) -> GoalRecord:
        self._validate_goal_id(goal_id)
        return self.store.load(goal_id)

    def _next_goal_id(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        candidate = self.store.allocate_goal_id(today)
        self._validate_goal_id(candidate)
        return candidate

    def _validate_goal_id(self, goal_id: str) -> None:
        if not GOAL_ID_PATTERN.match(goal_id):
            raise ValueError("Invalid goal id.")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def update_status(self, record: GoalRecord, status: str, phase: str | None = None, note: str | None = None) -> GoalRecord:
        status = status.strip().upper()
        current = record.status.strip().upper()
        if status != current:
            allowed = ALLOWED_STATUS_TRANSITIONS.get(current, set())
            if status not in allowed:
                raise ValueError(f"Invalid goal status transition: {current} -> {status}")
        now = self._now()
        notes = list(record.notes or [])
        if note:
            notes.append(note)
        updated = GoalRecord(
            goal_id=record.goal_id,
            goal=record.goal,
            repo=record.repo,
            status=status,
            created_at=record.created_at,
            updated_at=now,
            phase=phase or record.phase,
            utc_timestamp=record.utc_timestamp,
            goal_type=getattr(record, "goal_type", "CODE_MODIFICATION"),
            notes=notes,
        )
        self.store.save_transition(record, updated)
        self.store.append_jsonl(
            updated.goal_id,
            "history.jsonl",
            {
                "timestamp": now,
                "from": current,
                "to": status,
                "stage": phase or record.phase,
                "reason": note or "",
            },
        )
        try:
            from .metrics_service import GoalMetricsService

            GoalMetricsService(self).refresh_goal(updated.goal_id)
        except Exception:
            pass
        return updated
