from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BudgetTracker:
    max_total_input_tokens: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    provider_requests: int = 0
    retry_count: int = 0
    rate_limit_retry_count: int = 0
    service_unavailable_retry_count: int = 0
    repair_count: int = 0
    stage_regeneration_count: int = 0
    evidence_bytes: int = 0

    def add_usage(self, usage: dict[str, Any]) -> None:
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        self.retry_count += int(usage.get("retry_count") or 0)
        self.rate_limit_retry_count += int(usage.get("rate_limit_retry_count") or 0)
        self.service_unavailable_retry_count += int(usage.get("service_unavailable_retry_count") or 0)
        self.repair_count += int(usage.get("repair_count") or 0)
        self.provider_requests += int(usage.get("provider_requests") or 0)
        self.stage_regeneration_count += int(usage.get("stage_regeneration_count") or 0)

    def add_call(self) -> None:
        self.llm_calls += 1

    def add_failed_provider_request(self, kind: str | None = None, retry_count: int = 0) -> None:
        self.provider_requests += 1
        self.retry_count += int(retry_count or 0)
        if kind == "RATE_LIMIT":
            self.rate_limit_retry_count += int(retry_count or 0)
        if kind == "SERVICE_UNAVAILABLE":
            self.service_unavailable_retry_count += int(retry_count or 0)

    def can_run_next_agent(self) -> bool:
        if not self.max_total_input_tokens:
            return True
        if self.input_tokens == 0:
            return True
        return self.input_tokens < self.max_total_input_tokens

    def to_dict(self) -> dict[str, int | None]:
        return {
            "max_total_input_tokens": self.max_total_input_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "llm_calls": self.llm_calls,
            "provider_requests": self.provider_requests,
            "retry_count": self.retry_count,
            "rate_limit_retry_count": self.rate_limit_retry_count,
            "service_unavailable_retry_count": self.service_unavailable_retry_count,
            "repair_count": self.repair_count,
            "stage_regeneration_count": self.stage_regeneration_count,
            "evidence_bytes": self.evidence_bytes,
        }
