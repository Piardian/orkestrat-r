from __future__ import annotations

import time
import json
from typing import Any

from llm.client import BaseLLMClient, LLMError
from llm.execution import ExecutionModePolicy
from llm.structured import parse_or_repair_json
from schemas import Verdict


ANALYST_SYSTEM_PROMPT = """You are an evidence analyst.
You are reviewing an implementation PLAN or a READ-ONLY AUDIT request before any code has been changed.
Use only the supplied evidence packet.
Do not guess when evidence is missing.
Choose exactly one verdict: PASS, FAIL, UNKNOWN.
Return JSON only with: analyst, profile, verdict, confidence, reason, evidence, uncertainties.
Keep reason under 2 short sentences.
The evidence field must be a list of at most 3 objects: [{"path": "...", "lines": "..."}].
If there is no pre-implementation evidence, choose UNKNOWN and cite the closest inspected paths or summary.
Do not require an existing patch, modified files, implemented code, or proof that the requested change is already applied.
For read-only audit/inspection tasks (where no code modification is requested), choose PASS when the evidence packet provides sufficient repository and component context to conduct the audit and identify findings. Cite inspected source files in evidence.
For implementation tasks, evaluate whether the plan matches the goal, evidence supports the relevant files/symbols, the change is technically plausible, scope is bounded, and verification is sufficient.
"""


class Analyst:
    def __init__(self, client: BaseLLMClient, execution_policy: ExecutionModePolicy | None = None) -> None:
        self.client = client
        self.execution_policy = execution_policy or ExecutionModePolicy(name="default")

    def analyze(
        self,
        task: str,
        evidence_packet: dict[str, Any],
        analyst_id: str = "analyst",
        profile_id: str = "",
        perspective: str = "evidence-first evaluator",
        max_tokens: int = 1600,
    ) -> tuple[Verdict, dict]:
        compact = _compact_evidence(evidence_packet)
        user_prompt = json.dumps(
            {
                "analyst": analyst_id,
                "profile": profile_id,
                "perspective": perspective,
                "task": task,
                "evidence_packet": compact,
            },
            ensure_ascii=False,
        )
        start = time.monotonic()
        current_prompt = ANALYST_SYSTEM_PROMPT
        current_max_tokens = max(1, int(max_tokens))
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "retry_count": 0,
            "rate_limit_retry_count": 0,
            "service_unavailable_retry_count": 0,
            "provider_requests": 0,
            "stage_regeneration_count": 0,
        }
        for attempt in range(self.execution_policy.truncation_regenerations + 1):
            _enforce_stage_limits(start, int(usage.get("provider_requests") or 0) + 1, self.execution_policy)
            response = self.client.generate(current_prompt, user_prompt, max_tokens=current_max_tokens)
            usage["provider_requests"] = int(usage.get("provider_requests") or 0) + 1 + int(response.retry_count or 0)
            usage["input_tokens"] = _sum_tokens(usage.get("input_tokens"), response.input_tokens)
            usage["output_tokens"] = _sum_tokens(usage.get("output_tokens"), response.output_tokens)
            usage["retry_count"] = int(usage.get("retry_count") or 0) + int(response.retry_count or 0)
            usage["rate_limit_retry_count"] = int(usage.get("rate_limit_retry_count") or 0) + int(response.rate_limit_retry_count or 0)
            usage["service_unavailable_retry_count"] = int(usage.get("service_unavailable_retry_count") or 0) + int(response.service_unavailable_retry_count or 0)
            try:
                raw = parse_or_repair_json(
                    self.client,
                    response.text,
                    "analyst" if attempt == 0 else f"analyst-regeneration-{attempt}",
                    usage,
                    response.finish_reason,
                    max_repairs=self.execution_policy.json_repairs,
                    repair_max_tokens=current_max_tokens,
                    repair_timeout=120,
                )
                verdict = Verdict.from_dict(raw)
                return verdict, usage
            except LLMError as exc:
                if exc.kind != "TRUNCATED_MODEL_RESPONSE":
                    raise
                if attempt >= self.execution_policy.truncation_regenerations:
                    raise
                usage["stage_regeneration_count"] = int(usage.get("stage_regeneration_count") or 0) + 1
                current_prompt = (
                    ANALYST_SYSTEM_PROMPT
                    + "\nYour previous response was truncated. Re-evaluate the same evidence and return the required JSON schema only. Keep the response concise."
                )
                current_max_tokens = current_max_tokens if self.execution_policy.output_tokens == "provider_max" else current_max_tokens * 2
                continue
        raise LLMError("TRUNCATED_MODEL_RESPONSE", kind="TRUNCATED_MODEL_RESPONSE")


def _compact_evidence(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": packet.get("task"),
        "repository": packet.get("repository"),
        "repo_map": packet.get("repo_map"),
        "git": packet.get("git"),
        "searches": packet.get("searches"),
        "evidence": [
            {
                "path": item.get("path"),
                "lines": f"{item.get('line_start')}-{item.get('line_end')}",
                "content": item.get("content"),
            }
            for item in packet.get("evidence", [])
        ],
        "symbols": packet.get("symbols"),
        "summary": packet.get("summary"),
    }


def _sum_tokens(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return int(left or 0) + int(right or 0)


def _enforce_stage_limits(start: float, provider_requests: int, policy: ExecutionModePolicy) -> None:
    if provider_requests > policy.max_provider_requests_per_stage:
        raise LLMError("Provider request hard cap exceeded.", kind="PROVIDER_REQUEST_CAP")
    if time.monotonic() - start > policy.stage_timeout_seconds:
        raise LLMError("Stage timeout exceeded.", kind="STAGE_TIMEOUT")
