from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class BuilderRequest:
    goal_id: str
    goal: dict[str, Any]
    plan: dict[str, Any]
    review: dict[str, Any]
    complexity: dict[str, Any]
    evidence: dict[str, Any]
    mode: str
    builder_profile: str
    allowed_files: list[str]
    forbidden_patterns: list[str]
    forbidden_areas: list[str]
    acceptance_criteria: list[str]
    verification_commands: list[str]
    constraints: list[str]
    workspace_path: str
    target_repo: str
    allow_new_files: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BuilderResult:
    goal_id: str
    status: str
    failure_type: str | None
    recommended_executor: str
    changed_files: list[str]
    unauthorized_files: list[str]
    patch_path: str | None
    patch_size: int = 0
    verification_status: str | None = None
    verification_commands: list[str] | None = None
    verification_result: dict[str, Any] | None = None
    openhands_executed: bool = False
    terminal_tool_enabled: bool = False
    original_repo_modified: bool = False
    provider_requests: int = 0
    provider_retries: int = 0
    builder_rate_limit_waits: int = 0
    builder_rate_limit_wait_seconds: float = 0.0
    provider_429_count: int = 0
    provider_503_count: int = 0
    provider_timeout_count: int = 0
    quota_exhausted_count: int = 0
    retry_exhausted: bool = False
    preflight_warnings: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BuilderAdapter(Protocol):
    def execute(self, request: BuilderRequest) -> BuilderResult:
        raise NotImplementedError
