from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
import os
import json
import urllib.parse
import urllib.request

from registry import Profile


DEFAULT_EXECUTION_MODE = "default"
RELAXED_EXECUTION_MODE = "relaxed-acceptance"


@dataclass(frozen=True)
class ExecutionModePolicy:
    name: str
    output_tokens: str = "configured"
    truncation_regenerations: int = 1
    json_repairs: int = 1
    provider_retries: int = 2
    retry_wait_max_seconds: int = 60
    stage_timeout_seconds: int = 600
    max_provider_requests_per_stage: int = 10

    @property
    def relaxed(self) -> bool:
        return self.name == RELAXED_EXECUTION_MODE


def load_execution_policy(config: dict[str, Any], mode: str | None = None) -> ExecutionModePolicy:
    execution_modes = config.get("execution_modes", {}) or {}
    selected = str(mode or os.getenv("AGENT_CORE_EXECUTION_MODE") or DEFAULT_EXECUTION_MODE)
    raw = dict(execution_modes.get(selected) or execution_modes.get(DEFAULT_EXECUTION_MODE) or {})
    return ExecutionModePolicy(
        name=selected,
        output_tokens=str(raw.get("output_tokens", "configured")),
        truncation_regenerations=int(raw.get("truncation_regenerations", 1) or 1),
        json_repairs=int(raw.get("json_repairs", 1) or 1),
        provider_retries=int(raw.get("provider_retries", 2) or 2),
        retry_wait_max_seconds=int(raw.get("retry_wait_max_seconds", 60) or 60),
        stage_timeout_seconds=int(raw.get("stage_timeout_seconds", 600) or 600),
        max_provider_requests_per_stage=int(raw.get("max_provider_requests_per_stage", 10) or 10),
    )


def resolve_output_tokens(profile: Profile, requested_tokens: int, policy: ExecutionModePolicy, env_path: str | Path = ".env") -> int:
    requested = max(1, int(requested_tokens))
    if policy.output_tokens != "provider_max":
        return requested
    provider_max = resolve_provider_max_output_tokens(profile, env_path=env_path)
    if provider_max is None:
        return requested
    return max(1, int(provider_max))


@lru_cache(maxsize=32)
def resolve_provider_max_output_tokens(profile_id: str, model: str, provider: str, secret_env: str | None, env_value: str | None) -> int | None:
    if provider != "gemini":
        return None
    if not secret_env or not env_value:
        return None
    model_name = model.removeprefix("gemini/")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model_name)}?key={urllib.parse.quote(env_value)}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    for key in ("outputTokenLimit", "output_token_limit", "maxOutputTokens"):
        value = data.get(key)
        if isinstance(value, int) and value > 0:
            return value
    generation_config = data.get("generationConfig")
    if isinstance(generation_config, dict):
        for key in ("outputTokenLimit", "maxOutputTokens"):
            value = generation_config.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def provider_max_for_profile(profile: Profile, env_path: str | Path = ".env") -> int | None:
    env_value = os.getenv(profile.secret_env or "") if profile.secret_env else None
    return resolve_provider_max_output_tokens(profile.id, profile.model, profile.provider, profile.secret_env, env_value)
