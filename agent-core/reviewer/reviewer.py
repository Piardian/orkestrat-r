from __future__ import annotations

import time
import json
from typing import Any

from llm.client import BaseLLMClient, LLMError
from llm.execution import ExecutionModePolicy
from llm.structured import parse_or_repair_json
from schemas import Review, Verdict


REVIEWER_SYSTEM_PROMPT = """You are the final reviewer.
You are reviewing an implementation PLAN or a READ-ONLY AUDIT request before any code has been changed.
You will receive independent analyst results for the same evidence packet.
Your job:
- compare the results
- identify contradictions
- evaluate confidence levels
- choose UNKNOWN when evidence is insufficient
- rely only on the supplied analyst results and references
Do not guess.
Return JSON only with: final_verdict, confidence, agreement, reason, analysts, analyst_a, analyst_b, evidence, patch_required.
The analysts field must summarize all N-agent results as a list.
Set patch_required=false for PASS. Set patch_required=true for FAIL. Set patch_required=false for UNKNOWN.
Agreement values: FULL, PARTIAL, CONFLICT.
Do not require an existing patch, modified files, implemented code, or proof that the requested change is already applied.
For this pre-implementation review, approve (PASS) when the plan matches the goal, the evidence confirms the relevant files/symbols exist, the change is technically plausible, scope is bounded, and verification is sufficient.
For read-only audit tasks where no code modification is requested, approve (PASS) with patch_required=false when analysts agree that repository context is sufficient.
Use UNKNOWN only when pre-implementation evidence needed to judge the plan or audit is genuinely missing.
"""


class Reviewer:
    def __init__(self, client: BaseLLMClient, execution_policy: ExecutionModePolicy | None = None) -> None:
        self.client = client
        self.execution_policy = execution_policy or ExecutionModePolicy(name="default")

    def review(self, task: str, analyst_results: list[Verdict], status: str = "OK", max_tokens: int = 2400) -> tuple[Review, dict]:
        payload = {
            "task": task,
            "status": status,
            "analyst_results": [item.to_dict() for item in analyst_results],
        }
        user_prompt = json.dumps(payload, ensure_ascii=False)
        start = time.monotonic()
        current_prompt = REVIEWER_SYSTEM_PROMPT
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
                    "reviewer" if attempt == 0 else f"reviewer-regeneration-{attempt}",
                    usage,
                    response.finish_reason,
                    max_repairs=self.execution_policy.json_repairs,
                    repair_max_tokens=current_max_tokens,
                    repair_timeout=120,
                )
                review = Review.from_dict(raw)
                return review, usage
            except LLMError as exc:
                if exc.kind != "TRUNCATED_MODEL_RESPONSE":
                    raise
                if attempt >= self.execution_policy.truncation_regenerations:
                    raise
                usage["stage_regeneration_count"] = int(usage.get("stage_regeneration_count") or 0) + 1
                current_prompt = (
                    REVIEWER_SYSTEM_PROMPT
                    + "\nYour previous response was truncated. Re-evaluate the same analyst results and return the required JSON schema only. Keep the response concise."
                )
                current_max_tokens = current_max_tokens if self.execution_policy.output_tokens == "provider_max" else current_max_tokens * 2
                continue
        raise LLMError("TRUNCATED_MODEL_RESPONSE", kind="TRUNCATED_MODEL_RESPONSE")


def _sum_tokens(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return int(left or 0) + int(right or 0)


def _enforce_stage_limits(start: float, provider_requests: int, policy: ExecutionModePolicy) -> None:
    if provider_requests > policy.max_provider_requests_per_stage:
        raise LLMError("Provider request hard cap exceeded.", kind="PROVIDER_REQUEST_CAP")
    if time.monotonic() - start > policy.stage_timeout_seconds:
        raise LLMError("Stage timeout exceeded.", kind="STAGE_TIMEOUT")
