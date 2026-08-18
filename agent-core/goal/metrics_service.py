from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import json

from .metrics import GoalMetrics, GoalStageMetrics
from .service import GoalService


STAGE_NAMES = ["planning", "plan_review", "complexity", "builder", "codex_manual", "final_review", "apply"]

STATE_TO_STAGE = {
    "CREATED": "intake",
    "PLANNING": "planning",
    "PLANNED": "plan_review",
    "REVIEWING": "plan_review",
    "APPROVED": "plan_review",
    "COMPLEXITY_ASSESSING": "complexity",
    "READY_FOR_OPENHANDS": "builder",
    "BUILDING": "builder",
    "BUILT_PENDING_REVIEW": "builder",
    "BUILD_REVIEWING": "final_review",
    "READY_TO_APPLY": "apply",
    "APPLYING": "apply",
    "COMPLETED": "apply",
    "CODEX_REQUIRED": "codex_manual",
    "WAITING_CODEX": "codex_manual",
    "CODEX_RESPONSE_RECEIVED": "codex_manual",
    "CODEX_PATCH_STAGING": "codex_manual",
}


class GoalMetricsService:
    def __init__(self, service: GoalService | None = None, runtime_root: str | Path = "runtime") -> None:
        self.service = service or GoalService()
        self.runtime_root = Path(runtime_root)
        self.metrics_root = self.runtime_root / "metrics"
        self.metrics_root.mkdir(parents=True, exist_ok=True)

    def refresh_goal(self, goal_id: str) -> GoalMetrics:
        record = self.service.read_goal(goal_id)
        goal_dir = self.service.store.goal_dir(goal_id)
        metrics = self._build_goal_metrics(goal_id, record.to_dict(), goal_dir)
        self.service.store.save_plan(goal_id, "metrics.json", metrics.to_dict())
        self._append_event("goal", metrics.to_dict())
        return metrics

    def refresh_all(self) -> dict[str, Any]:
        goals = [self.refresh_goal(goal_id) for goal_id in self.service.store.list_goal_ids()]
        payload = self._build_global_metrics([item.to_dict() for item in goals])
        self.metrics_root.mkdir(parents=True, exist_ok=True)
        (self.metrics_root / "metrics.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self._append_event("global", payload)
        return payload

    def load_goal_metrics(self, goal_id: str) -> dict[str, Any] | None:
        path = self.service.store.goal_dir(goal_id) / "metrics.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_global_metrics(self) -> dict[str, Any]:
        path = self.metrics_root / "metrics.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return self._build_global_metrics([self.refresh_goal(goal_id).to_dict() for goal_id in self.service.store.list_goal_ids()])

    def _build_goal_metrics(self, goal_id: str, record: dict[str, Any], goal_dir: Path) -> GoalMetrics:
        history = self.service.store.read_jsonl(goal_id, "history.jsonl")
        duration_seconds = self._duration_seconds(record.get("created_at"), record.get("updated_at"))
        stages = self._stages_from_history(history)
        plan = self._load_json(goal_dir / "plan.json")
        review = self._load_optional_json(goal_dir / "review.json")
        complexity = self._load_optional_json(goal_dir / "complexity.json")
        build = self._load_optional_json(goal_dir / "build.json")
        codex = self._load_optional_json(goal_dir / "build.json") if (goal_dir / "codex_request.json").exists() else {}
        final_review = self._load_optional_json(goal_dir / "final_review.json")
        final_verification = self._load_optional_json(goal_dir / "final_verification.json")
        metrics = GoalMetrics(
            goal_id=goal_id,
            state=str(record.get("status", "UNKNOWN")),
            created_at=str(record.get("created_at", "")),
            updated_at=str(record.get("updated_at", "")),
            duration_seconds=duration_seconds,
            llm=self._llm_metrics(plan, review, complexity, build, final_review),
            providers=self._provider_health(plan, review, complexity, build, final_review),
            stages=stages,
            builder=self._builder_metrics(build, codex),
            verification=self._verification_metrics(build, final_verification),
            result=self._result_metrics(record, build, final_review),
            complexity=self._complexity_metrics(complexity),
            estimated_cost=self._estimated_cost(plan, review, complexity, build, final_review),
        )
        return metrics

    def _build_global_metrics(self, goals: list[dict[str, Any]]) -> dict[str, Any]:
        totals = {
            "goals_total": len(goals),
            "completed": 0,
            "failed": 0,
            "waiting_manual_action": 0,
            "openhands_builds": 0,
            "codex_builds": 0,
            "llm_logical_calls": 0,
            "provider_requests": 0,
            "retries": 0,
            "429": 0,
            "503": 0,
            "timeout": 0,
            "builder_rate_limit_waits": 0,
            "builder_rate_limit_wait_seconds": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }
        for goal in goals:
            state = str(goal.get("state", "")).upper()
            if state == "COMPLETED":
                totals["completed"] += 1
            elif state in {"WAITING_CODEX", "READY_TO_APPLY", "READY_FOR_OPENHANDS"}:
                totals["waiting_manual_action"] += 1
            elif state.endswith("FAILED") or state in {"BUILD_REJECTED", "BUILD_REVIEW_FAILED", "BUILD_REVIEW_UNKNOWN"}:
                totals["failed"] += 1
            if state in {"BUILT_PENDING_REVIEW", "BUILDING", "READY_FOR_OPENHANDS"}:
                totals["openhands_builds"] += 1
            if state in {"WAITING_CODEX", "CODEX_REQUIRED", "CODEX_RESPONSE_RECEIVED", "CODEX_PATCH_STAGING"}:
                totals["codex_builds"] += 1
            llm = goal.get("llm", {})
            totals["llm_logical_calls"] += int(llm.get("logical_calls") or 0)
            totals["provider_requests"] += int(llm.get("provider_requests") or 0)
            totals["retries"] += int(llm.get("provider_retries") or 0)
            providers = goal.get("providers", {})
            totals["429"] += int(providers.get("429") or 0)
            totals["503"] += int(providers.get("503") or 0)
            totals["timeout"] += int(providers.get("timeout") or 0)
            builder = goal.get("builder", {})
            totals["builder_rate_limit_waits"] += int(builder.get("builder_rate_limit_waits") or 0)
            totals["builder_rate_limit_wait_seconds"] += float(builder.get("builder_rate_limit_wait_seconds") or 0.0)
            totals["total_input_tokens"] += int(llm.get("input_tokens") or 0)
            totals["total_output_tokens"] += int(llm.get("output_tokens") or 0)
        return totals

    def _stages_from_history(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        stages: dict[str, GoalStageMetrics] = {}
        previous_timestamp = None
        previous_state = None
        for item in history:
            state = str(item.get("to", "")).upper()
            timestamp = str(item.get("timestamp", ""))
            stage_name = STATE_TO_STAGE.get(state)
            if stage_name and stage_name not in stages:
                stages[stage_name] = GoalStageMetrics(
                    started_at=timestamp,
                    completed_at=None,
                    duration_seconds=None,
                    status=state,
                    attempts=1,
                    failure_type=None,
                )
            elif stage_name:
                stage = stages[stage_name]
                stages[stage_name] = GoalStageMetrics(
                    started_at=stage.started_at,
                    completed_at=timestamp,
                    duration_seconds=_delta_seconds(stage.started_at, timestamp),
                    status=state,
                    attempts=stage.attempts + 1,
                    failure_type=stage.failure_type,
                )
            previous_timestamp = timestamp
            previous_state = state
        for name in STAGE_NAMES:
            if name not in stages:
                stages[name] = GoalStageMetrics(None, None, None, "UNKNOWN", 0, None)
        return {name: stage for name, stage in stages.items()}

    def _llm_metrics(self, plan: dict[str, Any], review: dict[str, Any], complexity: dict[str, Any], build: dict[str, Any], final_review: dict[str, Any]) -> dict[str, Any]:
        logical_calls = sum(
            int(item.get("logical_calls") or 0)
            for item in [plan, review, complexity, build, final_review]
            if isinstance(item, dict)
        )
        provider_requests = sum(
            int(item.get("provider_requests") or 0)
            for item in [plan, review, complexity, build, final_review]
            if isinstance(item, dict)
        )
        provider_retries = sum(
            int(item.get("provider_retries") or 0)
            for item in [plan, review, complexity, build, final_review]
            if isinstance(item, dict)
        )
        json_repairs = sum(
            int(item.get("json_repairs") or 0)
            for item in [plan, review, complexity, build, final_review]
            if isinstance(item, dict)
        )
        truncation_regeneration = sum(
            int(item.get("truncation_regeneration") or 0)
            for item in [plan, review, complexity, build, final_review]
            if isinstance(item, dict)
        )
        input_tokens = sum(int(item.get("input_tokens") or 0) for item in [plan, review, complexity, build, final_review] if isinstance(item, dict))
        output_tokens = sum(int(item.get("output_tokens") or 0) for item in [plan, review, complexity, build, final_review] if isinstance(item, dict))
        failed_requests = sum(int(item.get("failed_requests") or 0) for item in [plan, review, complexity, build, final_review] if isinstance(item, dict))
        return {
            "logical_calls": logical_calls,
            "provider_requests": provider_requests,
            "provider_retries": provider_retries,
            "json_repairs": json_repairs,
            "truncation_regeneration": truncation_regeneration,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "failed_requests": failed_requests,
        }

    def _provider_health(self, plan: dict[str, Any], review: dict[str, Any], complexity: dict[str, Any], build: dict[str, Any], final_review: dict[str, Any]) -> dict[str, Any]:
        metrics = {"429": 0, "503": 0, "timeout": 0, "malformed_json": 0, "truncated_response": 0, "other_error": 0}
        for item in [plan, review, complexity, build, final_review]:
            if not isinstance(item, dict):
                continue
            source = item.get("provider_failures") or item.get("provider_health") or {}
            if isinstance(source, dict):
                for key in metrics:
                    metrics[key] += int(source.get(key) or 0)
        return metrics

    def _builder_metrics(self, build: dict[str, Any], codex: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempts": int(build.get("provider_requests") or 0) if isinstance(build, dict) else 0,
            "successful_attempts": 1 if str(build.get("status", "")).upper() == "BUILT_PENDING_REVIEW" else 0,
            "provider_failures": int(build.get("provider_failures") or 0) if isinstance(build, dict) else 0,
            "policy_violations": 1 if str(build.get("status", "")).upper() == "BUILDER_POLICY_VIOLATION" else 0,
            "verification_failures": 1 if str(build.get("verification_status", "")).upper() == "FAIL" else 0,
            "changed_file_count": len([item for item in build.get("changed_files", []) if str(item).strip()]) if isinstance(build, dict) else 0,
            "patch_size": int(build.get("patch_size") or 0) if isinstance(build, dict) else 0,
            "runtime_seconds": 0,
            "manual_attempts": int(codex.get("manual_attempts") or 0) if isinstance(codex, dict) else 0,
            "provider_requests": int(build.get("provider_requests") or 0) if isinstance(build, dict) else 0,
            "provider_retries": int(build.get("provider_retries") or 0) if isinstance(build, dict) else 0,
            "builder_rate_limit_waits": int(build.get("builder_rate_limit_waits") or 0) if isinstance(build, dict) else 0,
            "builder_rate_limit_wait_seconds": float(build.get("builder_rate_limit_wait_seconds") or 0.0) if isinstance(build, dict) else 0.0,
            "provider_429_count": int(build.get("provider_429_count") or 0) if isinstance(build, dict) else 0,
            "provider_503_count": int(build.get("provider_503_count") or 0) if isinstance(build, dict) else 0,
            "provider_timeout_count": int(build.get("provider_timeout_count") or 0) if isinstance(build, dict) else 0,
            "quota_exhausted_count": int(build.get("quota_exhausted_count") or 0) if isinstance(build, dict) else 0,
            "preflight_warning_count": len(build.get("preflight_warnings") or []) if isinstance(build, dict) else 0,
        }

    def _verification_metrics(self, build: dict[str, Any], final_verification: dict[str, Any]) -> dict[str, Any]:
        verification = build.get("verification_result") if isinstance(build, dict) else {}
        if not isinstance(verification, dict):
            verification = {}
        if not isinstance(final_verification, dict):
            final_verification = {}
        return {
            "status": str((final_verification or verification).get("status", build.get("verification_status", "UNKNOWN"))),
            "exit_code": int((final_verification or verification).get("exit_code", -1) or -1),
        }

    def _result_metrics(self, record: dict[str, Any], build: dict[str, Any], final_review: dict[str, Any]) -> dict[str, Any]:
        executor = "unknown"
        if isinstance(build, dict):
            executor = str(build.get("recommended_executor", build.get("executor", "unknown")))
        if isinstance(final_review, dict) and final_review.get("executor"):
            executor = str(final_review.get("executor"))
        return {
            "executor": executor,
            "final_status": str(record.get("status", "UNKNOWN")),
            "goal_phase": str(record.get("phase", "UNKNOWN")),
        }

    def _complexity_metrics(self, complexity: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(complexity, dict):
            return {}
        return {
            "score": int(complexity.get("score") or 0),
            "severity": str(complexity.get("severity", "UNKNOWN")),
            "recommended_executor": str(complexity.get("recommended_executor", "unknown")),
            "actual_executor": str(complexity.get("actual_executor", complexity.get("recommended_executor", "unknown"))),
        }

    def _estimated_cost(self, plan: dict[str, Any], review: dict[str, Any], complexity: dict[str, Any], build: dict[str, Any], final_review: dict[str, Any]) -> str | float:
        total_input = self._sum_tokens(plan, review, complexity, build, final_review, key="input_tokens")
        total_output = self._sum_tokens(plan, review, complexity, build, final_review, key="output_tokens")
        if total_input == 0 and total_output == 0:
            return "UNKNOWN"
        return "UNKNOWN"

    def _sum_tokens(self, *items: dict[str, Any], key: str) -> int:
        return sum(int(item.get(key) or 0) for item in items if isinstance(item, dict))

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_optional_json(self, path: Path) -> dict[str, Any]:
        return self._load_json(path)

    def _duration_seconds(self, started_at: str | None, completed_at: str | None) -> float:
        if not started_at or not completed_at:
            return 0.0
        return max(0.0, _delta_seconds(started_at, completed_at) or 0.0)

    def _append_event(self, kind: str, payload: dict[str, Any]) -> None:
        self.metrics_root.mkdir(parents=True, exist_ok=True)
        path = self.metrics_root / "events.jsonl"
        line = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _delta_seconds(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    try:
        start = datetime.fromisoformat(left)
        end = datetime.fromisoformat(right)
    except ValueError:
        return 0.0
    return max(0.0, (end - start).total_seconds())
