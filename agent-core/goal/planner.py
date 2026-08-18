from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import yaml

from commander import Commander
from evidence.builder import EvidenceBuilder
from llm.client import BaseLLMClient, LLMError
from llm.execution import ExecutionModePolicy, load_execution_policy, resolve_output_tokens
from llm.router import LLMRouter
from llm.structured import parse_or_repair_json
from routing import resolve_commander_profile
from schemas import SearchPlan

from .audit_report import is_read_only_audit
from .metrics_service import GoalMetricsService
from .model import GoalRecord
from .plan import GoalPlan, normalize_string_list
from .service import GoalService
from .store import GoalStore


IMPLEMENTATION_PLAN_PROMPT = """You are a plan compiler.
Use only the supplied goal and bounded evidence packet.
Return JSON only with:
plan_version: integer
goal_id: string
objective: string
summary: string
tasks: array of objects
candidate_files: array of strings (files to read/investigate)
allowed_files: array of strings (files permitted to be modified)
acceptance_criteria: array of strings
verification: array of commands (each command must be a full executable string or argv array, prefer 'python -m unittest test_file.py' or python commands; do not rely on bash/grep/sed)
risks: array of strings
constraints: array of strings
patch_expected: boolean
uncertainties: array of strings
evidence_refs: array of strings

Do not invent files or facts not supported by evidence.
If evidence is insufficient, use UNKNOWN in summary or uncertainties.
Keep the response concise and bounded.
"""


class GoalPlanner:
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

    def plan_goal(self, goal_id: str, commander_profile: str | None = None) -> tuple[GoalRecord, SearchPlan, dict[str, Any], GoalPlan, dict[str, int]]:
        record = self.service.read_goal(goal_id)
        record = self.service.update_status(record, "PLANNING", phase="search-planning", note="planner started")
        resolved_profile = resolve_commander_profile(commander_profile)
        registry = getattr(self.router, "registry", None)
        profile = registry.get(resolved_profile) if registry else None
        client = self._get_client(resolved_profile)

        try:
            search_plan, search_usage = Commander(client, self._policy).create_plan(
                record.goal,
                Path(record.repo).name,
                max_tokens=resolve_output_tokens(profile, 256, self._policy) if profile else 256,
            )
            evidence_packet = EvidenceBuilder(record.repo, search_plan.to_dict()).build()
            implementation_plan, impl_usage = self._build_implementation_plan(client, record, evidence_packet, profile)

            # Persist plan artifacts before publishing PLANNED. PostgreSQL treats
            # save_transition as the guarded source-of-truth state change, while
            # plain save intentionally refuses cross-status overwrites.
            self.service.store.save_plan(record.goal_id, "search_plan.json", search_plan.to_dict())
            self.service.store.save_plan(record.goal_id, "evidence.json", evidence_packet)
            self.service.store.save_plan(record.goal_id, "plan.json", implementation_plan.to_dict())
            planned_record = self.service.update_status(
                record,
                "PLANNED",
                phase="planned",
                note="planning completed",
            )
            GoalMetricsService(self.service).refresh_goal(planned_record.goal_id)

            usage = {
                "logical_calls": 2,
                "provider_requests": int(search_usage.get("provider_requests") or 0) + int(impl_usage.get("provider_requests") or 0),
                "provider_retries": int(search_usage.get("retry_count") or 0) + int(impl_usage.get("retry_count") or 0),
                "json_repairs": int(search_usage.get("repair_count") or 0) + int(impl_usage.get("repair_count") or 0),
                "truncation_regeneration": int(search_usage.get("truncation_regeneration") or 0),
                "input_tokens": int(search_usage.get("input_tokens") or 0) + int(impl_usage.get("input_tokens") or 0),
                "output_tokens": int(search_usage.get("output_tokens") or 0) + int(impl_usage.get("output_tokens") or 0),
            }
            return planned_record, search_plan, evidence_packet, implementation_plan, usage
        except Exception as exc:
            failed = self.service.update_status(record, "PLANNING_FAILED", phase="planning-failed", note=str(exc))
            GoalMetricsService(self.service).refresh_goal(failed.goal_id)
            raise

    def _build_implementation_plan(
        self,
        client: BaseLLMClient,
        record: GoalRecord,
        evidence_packet: dict[str, Any],
        profile: Any,
    ) -> tuple[GoalPlan, dict[str, int]]:
        payload = {
            "goal": record.to_dict(),
            "evidence": {
                "summary": evidence_packet.get("summary", {}),
                "files_inspected": evidence_packet.get("summary", {}).get("files_inspected", 0),
                "evidence_refs": [
                    f"{item.get('path')}:{item.get('line_start')}-{item.get('line_end')}"
                    for item in evidence_packet.get("evidence", [])[:5]
                    if isinstance(item, dict)
                ],
                "manifest_sample": evidence_packet.get("manifest_sample", [])[:10],
            },
        }
        response = client.generate(
            IMPLEMENTATION_PLAN_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            max_tokens=resolve_output_tokens(profile, 2200, self._policy) if profile else 2200,
            temperature=0.0,
            chat_template_kwargs={"enable_thinking": False},
            timeout=300,
        )
        usage = {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "retry_count": response.retry_count,
            "rate_limit_retry_count": response.rate_limit_retry_count,
            "service_unavailable_retry_count": response.service_unavailable_retry_count,
            "provider_requests": 1 + int(response.retry_count or 0),
        }
        raw = parse_or_repair_json(
            client,
            response.text,
            "implementation-plan",
            usage,
            response.finish_reason,
            max_repairs=self._policy.json_repairs,
            repair_max_tokens=resolve_output_tokens(profile, 2200, self._policy) if profile else 2200,
            repair_timeout=120,
        )
        plan = self._validate_plan(raw, record.goal_id, record.goal)
        return plan, usage

    def _load_config(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def _get_client(self, profile_id: str) -> BaseLLMClient:
        try:
            return self.router.get_client(profile_id, self._policy)
        except TypeError:
            return self.router.get_client(profile_id)

    def _validate_plan(self, raw: dict[str, Any], goal_id: str, objective: str) -> GoalPlan:
        plan_version_raw = raw.get("plan_version", 1)
        try:
            if isinstance(plan_version_raw, str) and "." in plan_version_raw:
                plan_version = int(plan_version_raw.split(".")[0])
            else:
                plan_version = int(float(plan_version_raw))
        except (TypeError, ValueError):
            plan_version = 1
        if plan_version < 1:
            plan_version = 1
        tasks = raw.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("tasks must be a list.")
        candidate_files = normalize_string_list(raw.get("candidate_files"))
        allowed_files = normalize_string_list(raw.get("allowed_files"))
        acceptance_criteria = normalize_string_list(raw.get("acceptance_criteria"))
        verification = normalize_string_list(raw.get("verification"))
        risks = normalize_string_list(raw.get("risks"))
        constraints = normalize_string_list(raw.get("constraints"))
        uncertainties = normalize_string_list(raw.get("uncertainties"))
        evidence_refs = normalize_string_list(raw.get("evidence_refs"))
        summary = str(raw.get("summary", "")).strip()
        if not summary:
            raise ValueError("summary cannot be empty.")
        is_audit = is_read_only_audit(objective)
        patch_expected = False if is_audit else bool(raw.get("patch_expected", True))
        if is_audit:
            allowed_files = []
        return GoalPlan(
            plan_version=plan_version,
            goal_id=goal_id,
            objective=objective,
            summary=summary,
            tasks=[dict(item) for item in tasks if isinstance(item, dict)],
            candidate_files=candidate_files,
            allowed_files=allowed_files,
            acceptance_criteria=acceptance_criteria,
            verification=verification,
            risks=risks,
            constraints=constraints,
            patch_expected=patch_expected,
            uncertainties=uncertainties,
            evidence_refs=evidence_refs,
        )
