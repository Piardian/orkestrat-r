from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from analyst import Analyst
from llm.client import BaseLLMClient, LLMError
from llm.execution import load_execution_policy, resolve_output_tokens
from llm.router import LLMRouter
from reviewer import Reviewer
from schemas import Review, Verdict

from .model import GoalRecord
from .metrics_service import GoalMetricsService
from .review import GoalReview
from .service import GoalService
from .audit_report import generate_audit_report, is_read_only_audit


ANALYST_SPECS = [
    ("analyst-1", "gemini-user-b", "evidence-first evaluator"),
    ("analyst-2", "gemini-user-c", "skeptical verifier"),
    ("analyst-3", "gemini-user-a", "adversarial reviewer"),
]


class GoalReviewService:
    def __init__(
        self,
        service: GoalService | None = None,
        router: LLMRouter | None = None,
        config_path: str | Path = "config/army.yaml",
        execution_mode: str | None = None,
    ) -> None:
        self.service = service or GoalService()
        self.router = router or LLMRouter()
        self.config_path = Path(config_path)
        self.execution_mode = execution_mode
        self._config = self._load_config(self.config_path)
        self._policy = load_execution_policy(self._config, execution_mode)

    def review_goal(
        self,
        goal_id: str,
        reviewer_profile: str = "gemini-user-d",
    ) -> tuple[GoalRecord, list[Verdict], Review, GoalReview, dict[str, int]]:
        record = self.service.read_goal(goal_id)
        self._ensure_planned(record)
        record = self.service.update_status(record, "REVIEWING", phase="reviewing", note="review started")

        base_dir = self.service.store.goal_dir(goal_id)
        _ = self._load_json(base_dir / "search_plan.json")
        evidence_packet = self._load_json(base_dir / "evidence.json")
        _ = self._load_json(base_dir / "plan.json")

        analyst_results: list[Verdict] = []
        analyst_payloads: list[dict[str, Any]] = []
        usage = _empty_usage()
        provider_health = _empty_provider_health()

        try:
            for analyst_id, profile_id, perspective in ANALYST_SPECS:
                registry = getattr(self.router, "registry", None)
                profile = registry.get(profile_id) if registry else None
                client = self._get_client(profile_id)
                verdict, analyst_usage = Analyst(client, self._policy).analyze(
                    task=record.goal,
                    evidence_packet=evidence_packet,
                    analyst_id=analyst_id,
                    profile_id=profile_id,
                    perspective=perspective,
                    max_tokens=resolve_output_tokens(profile, 1600, self._policy) if profile else 1600,
                )
                usage = _merge_usage(usage, analyst_usage)
                if not verdict.analyst:
                    verdict = Verdict(
                        verdict=verdict.verdict,
                        confidence=verdict.confidence,
                        reason=verdict.reason,
                        evidence=verdict.evidence,
                        analyst=analyst_id,
                        profile=profile_id,
                        uncertainties=verdict.uncertainties,
                    )
                analyst_results.append(verdict)
                analyst_payloads.append(verdict.to_dict())

            if len(analyst_results) < 2:
                final_status = "REVIEW_FAILED"
                review = Review.from_dict(
                    {
                        "final_verdict": "UNKNOWN",
                        "confidence": 0.0,
                        "agreement": "CONFLICT",
                        "reason": "Insufficient analyst results.",
                        "analyst_a": analyst_payloads[0] if analyst_payloads else {},
                        "analyst_b": analyst_payloads[1] if len(analyst_payloads) > 1 else None,
                        "analysts": analyst_payloads,
                        "evidence": [],
                        "patch_required": False,
                    }
                )
                goal_review = self._build_goal_review(goal_id, record.goal, reviewer_profile, final_status, review, analyst_payloads, analyst_results, evidence_packet, usage, reviewer_result={})
                updated = self.service.update_status(record, final_status, phase="review-failed", note="insufficient analyst results")
                self.service.store.save_review_bundle(updated, analyst_payloads, goal_review)
                GoalMetricsService(self.service).refresh_goal(updated.goal_id)
                return updated, analyst_results, review, goal_review, usage

            reviewer_registry = getattr(self.router, "registry", None)
            reviewer_profile_obj = reviewer_registry.get(reviewer_profile) if reviewer_registry else None
            reviewer_client = self._get_client(reviewer_profile)
            review_result, reviewer_usage = Reviewer(reviewer_client, self._policy).review(
                record.goal,
                analyst_results,
                status="OK",
                max_tokens=resolve_output_tokens(reviewer_profile_obj, 2400, self._policy) if reviewer_profile_obj else 2400,
            )
            usage = _merge_usage(usage, reviewer_usage)
            plan_data = self._load_json(base_dir / "plan.json")
            is_audit = (getattr(record, "goal_type", "") == "READ_ONLY_AUDIT") or (plan_data.get("patch_expected") is False) or is_read_only_audit(record.goal, plan_data)

            if is_audit:
                report_text = generate_audit_report(
                    goal_id=goal_id,
                    goal_text=record.goal,
                    repo_path=record.repo,
                    plan_dict=plan_data,
                    evidence_packet=evidence_packet,
                    analyst_results=analyst_results,
                    review_dict=review_result.to_dict(),
                )
                self.service.store.save_text(goal_id, "audit_report.md", report_text)
                if review_result.final_verdict == "PASS":
                    final_status = "COMPLETED"
                    phase_name = "audit-completed"
                    note_msg = "audit report generated; read-only audit completed"
                else:
                    final_status = _final_status_for_review(review_result)
                    phase_name = "reviewed"
                    note_msg = f"review verdict={review_result.final_verdict}"
            else:
                final_status = _final_status_for_review(review_result)
                phase_name = "reviewed"
                note_msg = f"review verdict={review_result.final_verdict}"

            review = review_result
            goal_review = self._build_goal_review(goal_id, record.goal, reviewer_profile, final_status, review, analyst_payloads, analyst_results, evidence_packet, usage, provider_health, reviewer_result=review.to_dict())

            updated = self.service.update_status(record, final_status, phase=phase_name, note=note_msg)
            self.service.store.save_review_bundle(updated, analyst_payloads, goal_review)
            GoalMetricsService(self.service).refresh_goal(updated.goal_id)
            return updated, analyst_results, review, goal_review, usage
        except Exception as exc:
            _merge_usage_from_error(usage, exc)
            _merge_provider_health_from_error(provider_health, exc)
            failed = self.service.update_status(record, "REVIEW_FAILED", phase="review-failed", note=str(exc))
            self.service.store.save_review_bundle(
                failed,
                analyst_payloads,
                GoalReview(
                    goal_id=goal_id,
                    task=record.goal,
                    reviewer_profile=reviewer_profile,
                    status="REVIEW_FAILED",
                    agreement="CONFLICT",
                    final_verdict="UNKNOWN",
                    confidence=0.0,
                    reason=str(exc),
                    patch_required=False,
                    analyst_results=analyst_payloads,
                    reviewer_result={},
                    evidence_refs=_collect_evidence_refs(analyst_results, evidence_packet) if "evidence_packet" in locals() else [],
                    provider_requests=usage["provider_requests"],
                    logical_calls=4,
                    provider_retries=usage["provider_retries"],
                    json_repairs=usage["json_repairs"],
                    stage_regenerations=usage["stage_regenerations"],
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    review_429_count=provider_health["429"],
                    review_503_count=provider_health["503"],
                    review_timeout_count=provider_health["timeout"],
                    provider_health=dict(provider_health),
                ),
            )
            GoalMetricsService(self.service).refresh_goal(failed.goal_id)
            raise

    def _ensure_planned(self, record: GoalRecord) -> None:
        if record.status.strip().upper() != "PLANNED":
            raise ValueError("Goal must be PLANNED before review.")

    def _build_goal_review(
        self,
        goal_id: str,
        task: str,
        reviewer_profile: str,
        status: str,
        review: Review,
        analyst_payloads: list[dict[str, Any]],
        analyst_results: list[Verdict],
        evidence_packet: dict[str, Any],
        usage: dict[str, int],
        provider_health: dict[str, int],
        reviewer_result: dict[str, Any],
    ) -> GoalReview:
        return GoalReview(
            goal_id=goal_id,
            task=task,
            reviewer_profile=reviewer_profile,
            status=status,
            agreement=review.agreement,
            final_verdict=review.final_verdict,
            confidence=review.confidence,
            reason=review.reason,
            patch_required=review.patch_required,
            analyst_results=analyst_payloads,
            reviewer_result=reviewer_result,
            evidence_refs=_collect_evidence_refs(analyst_results, evidence_packet),
            provider_requests=usage["provider_requests"],
            logical_calls=4,
            provider_retries=usage["provider_retries"],
            json_repairs=usage["json_repairs"],
            stage_regenerations=usage["stage_regenerations"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            review_429_count=provider_health["429"],
            review_503_count=provider_health["503"],
            review_timeout_count=provider_health["timeout"],
            provider_health=dict(provider_health),
        )

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        data = path.read_text(encoding="utf-8")
        return json.loads(data)

    def _load_config(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def _get_client(self, profile_id: str) -> BaseLLMClient:
        try:
            return self.router.get_client(profile_id, self._policy)
        except TypeError:
            return self.router.get_client(profile_id)


def _collect_evidence_refs(analyst_results: list[Verdict], evidence_packet: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for verdict in analyst_results:
        for item in verdict.evidence:
            ref = f"{item.get('path')}:{item.get('lines')}"
            if ref not in refs:
                refs.append(ref)
    for item in evidence_packet.get("evidence", [])[:10]:
        if isinstance(item, dict):
            ref = f"{item.get('path')}:{item.get('line_start')}-{item.get('line_end')}"
            if ref not in refs:
                refs.append(ref)
    return refs[:20]


def _final_status_for_review(review: Review) -> str:
    if review.final_verdict == "PASS":
        return "APPROVED"
    if review.final_verdict == "FAIL":
        return "REVISION_REQUIRED" if review.patch_required else "REVIEW_FAILED"
    if review.final_verdict == "UNKNOWN":
        return "REVIEW_UNKNOWN"
    return "REVIEW_FAILED"


def _empty_usage() -> dict[str, int]:
    return {
        "provider_requests": 0,
        "provider_retries": 0,
        "json_repairs": 0,
        "stage_regenerations": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _empty_provider_health() -> dict[str, int]:
    return {"429": 0, "503": 0, "timeout": 0, "malformed_json": 0, "truncated_response": 0, "other_error": 0}


def _merge_usage(base: dict[str, int], usage: dict[str, Any]) -> dict[str, int]:
    base = dict(base)
    base["provider_requests"] += int(usage.get("provider_requests") or 0)
    base["provider_retries"] += int(usage.get("retry_count") or 0)
    base["json_repairs"] += int(usage.get("repair_count") or 0)
    base["stage_regenerations"] += int(usage.get("stage_regeneration_count") or 0)
    base["input_tokens"] += int(usage.get("input_tokens") or 0)
    base["output_tokens"] += int(usage.get("output_tokens") or 0)
    return base


def _merge_provider_health(base: dict[str, int], usage: dict[str, Any]) -> dict[str, int]:
    return base


def _merge_provider_health_from_error(base: dict[str, int], exc: Exception) -> dict[str, int]:
    kind = getattr(exc, "kind", "")
    message = str(exc).lower()
    if kind == "SERVICE_UNAVAILABLE" or "503" in message:
        base["503"] += 1
    elif kind in {"RATE_LIMIT", "QUOTA_EXHAUSTED"} or "429" in message:
        base["429"] += 1
    elif kind in {"CONNECTION_ERROR", "NETWORK_ERROR", "STAGE_TIMEOUT"} or "timeout" in message:
        base["timeout"] += 1
    else:
        base["other_error"] += 1
    return base


def _merge_usage_from_error(base: dict[str, int], exc: Exception) -> dict[str, int]:
    retry_count = int(getattr(exc, "retry_count", 0) or 0)
    if isinstance(exc, LLMError):
        base["provider_requests"] += retry_count + 1
        base["provider_retries"] += retry_count
    return base
