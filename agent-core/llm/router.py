from __future__ import annotations

from pathlib import Path
import os

from dotenv import load_dotenv

from registry import ProfileRegistry

from .client import BaseLLMClient, GeminiClient, LLMError, OpenAICompatibleClient
from .execution import ExecutionModePolicy


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class LLMRouter:
    def __init__(self, config_path: str | Path = "config/profiles.yaml", env_path: str | Path = ".env") -> None:
        load_dotenv(dotenv_path=env_path, override=False)
        self.registry = ProfileRegistry(config_path)

    def get_client(self, profile_id: str, execution_policy: ExecutionModePolicy | None = None) -> BaseLLMClient:
        profile = self.registry.get(profile_id)
        if not profile.enabled:
            raise ValueError(f"Profile is disabled: {profile.id}")
        max_retries = int(execution_policy.provider_retries if execution_policy else 2)
        retry_wait_cap_seconds = int(execution_policy.retry_wait_max_seconds if execution_policy else 60)

        if _truthy(os.getenv("AGENT_ARMY_LITELLM_ENABLED")):
            base_url = os.getenv("AGENT_ARMY_LITELLM_PROXY_URL", "http://127.0.0.1:4000/v1").rstrip("/")
            proxy_key = os.getenv("AGENT_ARMY_LITELLM_API_KEY", "local-agent-army")
            # LiteLLM exposes each existing profile id as an OpenAI-compatible model alias.
            # Provider credentials stay in the gateway process rather than each agent process.
            return OpenAICompatibleClient(
                profile.id,
                proxy_key,
                base_url,
                max_retries=max_retries,
                retry_wait_cap_seconds=retry_wait_cap_seconds,
            )

        api_key = os.getenv(profile.secret_env or "") if profile.secret_env else None
        if profile.secret_env and not api_key:
            raise LLMError(f"Secret env is not configured for profile: {profile.id}", kind="MISSING_SECRET")
        if profile.provider == "gemini":
            return GeminiClient(profile.model, api_key or "", max_retries=max_retries, retry_wait_cap_seconds=retry_wait_cap_seconds)
        if profile.provider == "openai-compatible":
            if not profile.base_url:
                raise ValueError(f"base_url is required for profile: {profile.id}")
            return OpenAICompatibleClient(profile.model, api_key or "", profile.base_url, max_retries=max_retries, retry_wait_cap_seconds=retry_wait_cap_seconds)
        raise NotImplementedError(f"Provider not implemented yet: {profile.provider}")
