from __future__ import annotations

import time
from typing import Any

from llm.client import BaseLLMClient, LLMError
from llm.execution import ExecutionModePolicy
from llm.structured import parse_model_json, parse_or_repair_json
from schemas import SearchPlan


COMMANDER_SYSTEM_PROMPT = """Sen bir search planner'sın.
Repo üzerinde doğrudan araştırma yapma.
Görevi çözmek için deterministic araçların uygulayabileceği küçük ve kontrollü bir arama planı üret.
Kurallar:
- maksimum 5 search term
- maksimum 5 hedef dosya
- maksimum 80 satır/dosya
- recursive içerik dump isteme
- repo'nun tamamını okuma isteme
- secret dosyaları isteme
- yalnızca JSON döndür
JSON alanları: task, search_terms, max_files, max_lines_per_file.
"""


class Commander:
    def __init__(self, client: BaseLLMClient, execution_policy: ExecutionModePolicy | None = None) -> None:
        self.client = client
        self.execution_policy = execution_policy or ExecutionModePolicy(name="default")

    def create_plan(self, task: str, repo_name: str, max_tokens: int = 256) -> tuple[SearchPlan, dict[str, Any]]:
        user_prompt = f"Task: {task}\nRepository name: {repo_name}\nKüçük search_plan JSON üret."
        start = time.monotonic()
        requested_max_tokens = max(1, int(max_tokens))
        current_prompt = COMMANDER_SYSTEM_PROMPT
        current_max_tokens = requested_max_tokens
        usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "retry_count": 0,
            "rate_limit_retry_count": 0,
            "service_unavailable_retry_count": 0,
            "provider_requests": 0,
            "truncation_regeneration": 0,
            "finish_reason": None,
        }
        for attempt in range(self.execution_policy.truncation_regenerations + 1):
            _enforce_stage_limits(start, int(usage.get("provider_requests") or 0) + 1, self.execution_policy)
            response = self.client.generate(
                current_prompt,
                user_prompt,
                max_tokens=current_max_tokens,
                temperature=0.0,
                chat_template_kwargs={"enable_thinking": False},
                timeout=300,
            )
            response_usage: dict[str, Any] = _usage_from_response(response)
            usage = _merge_usage(usage, response_usage)
            usage["finish_reason"] = response.finish_reason
            try:
                raw = parse_model_json(response.text, "commander" if attempt == 0 else f"commander-regeneration-{attempt}", response.finish_reason)
                plan = SearchPlan.from_dict(raw, fallback_task=task)
                return _attach_plan_meta(plan, usage, response.finish_reason, int(usage.get("truncation_regeneration") or 0)), usage
            except LLMError as exc:
                if exc.kind != "TRUNCATED_MODEL_RESPONSE":
                    raw = parse_or_repair_json(
                        self.client,
                        response.text,
                        "commander" if attempt == 0 else f"commander-regeneration-{attempt}",
                        usage,
                        response.finish_reason,
                        max_repairs=self.execution_policy.json_repairs,
                        repair_max_tokens=current_max_tokens,
                        repair_timeout=120,
                    )
                    plan = SearchPlan.from_dict(raw, fallback_task=task)
                    return _attach_plan_meta(plan, usage, response.finish_reason, int(usage.get("truncation_regeneration") or 0)), usage
                if attempt >= self.execution_policy.truncation_regenerations:
                    raise
                usage["truncation_regeneration"] = int(usage.get("truncation_regeneration") or 0) + 1
                current_prompt = COMMANDER_SYSTEM_PROMPT + "\nÖnceki yanıtın kesildi. Aynı JSON şemasını kısa ve tam ver."
                current_max_tokens = _bounded_regen_tokens(current_max_tokens, self.execution_policy)
                continue
        raise LLMError("TRUNCATED_MODEL_RESPONSE", kind="TRUNCATED_MODEL_RESPONSE")


def _usage_from_response(response: Any) -> dict[str, Any]:
    return {
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "retry_count": response.retry_count,
        "rate_limit_retry_count": response.rate_limit_retry_count,
        "service_unavailable_retry_count": response.service_unavailable_retry_count,
        "provider_requests": 1,
        "truncation_regeneration": 0,
        "finish_reason": str(response.finish_reason) if response.finish_reason else None,
    }


def _merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    merged["input_tokens"] = _sum_tokens(merged.get("input_tokens"), right.get("input_tokens"))
    merged["output_tokens"] = _sum_tokens(merged.get("output_tokens"), right.get("output_tokens"))
    merged["retry_count"] = int(merged.get("retry_count") or 0) + int(right.get("retry_count") or 0)
    merged["rate_limit_retry_count"] = int(merged.get("rate_limit_retry_count") or 0) + int(right.get("rate_limit_retry_count") or 0)
    merged["service_unavailable_retry_count"] = int(merged.get("service_unavailable_retry_count") or 0) + int(right.get("service_unavailable_retry_count") or 0)
    merged["provider_requests"] = int(merged.get("provider_requests") or 0) + int(right.get("provider_requests") or 0)
    merged["truncation_regeneration"] = int(merged.get("truncation_regeneration") or 0) + int(right.get("truncation_regeneration") or 0)
    merged["finish_reason"] = right.get("finish_reason") or merged.get("finish_reason")
    return merged


def _attach_plan_meta(plan: SearchPlan, usage: dict[str, Any], finish_reason: str | None, truncation_regeneration: int) -> SearchPlan:
    return SearchPlan(
        task=plan.task,
        search_terms=list(plan.search_terms),
        max_search_results=plan.max_search_results,
        max_files=plan.max_files,
        max_lines_per_file=plan.max_lines_per_file,
        max_chars_per_file=plan.max_chars_per_file,
        max_command_output_lines=plan.max_command_output_lines,
        max_test_output_lines=plan.max_test_output_lines,
        finish_reason=finish_reason or str(usage.get("finish_reason") or "") or None,
        truncation_regeneration=truncation_regeneration,
    )


def _sum_tokens(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return int(left or 0) + int(right or 0)


def _bounded_regen_tokens(initial_tokens: int, policy: ExecutionModePolicy) -> int:
    if policy.output_tokens == "provider_max":
        return initial_tokens
    if initial_tokens <= 0:
        return 1
    return min(initial_tokens * 2, max(initial_tokens, 4096))


def _enforce_stage_limits(start: float, provider_requests: int, policy: ExecutionModePolicy) -> None:
    if provider_requests > policy.max_provider_requests_per_stage:
        raise LLMError("Provider request hard cap exceeded.", kind="PROVIDER_REQUEST_CAP")
    elapsed = time.monotonic() - start
    if elapsed > policy.stage_timeout_seconds:
        raise LLMError("Stage timeout exceeded.", kind="STAGE_TIMEOUT")
