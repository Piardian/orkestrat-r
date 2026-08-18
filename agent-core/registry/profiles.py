from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os

import yaml


@dataclass(frozen=True)
class Profile:
    id: str
    provider: str
    model: str
    base_url: str | None
    secret_env: str | None
    owner: str
    enabled: bool
    role: str

    @property
    def secret_configured(self) -> bool:
        if not self.secret_env:
            return True
        return bool(os.getenv(self.secret_env))

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "secret_env": self.secret_env,
            "secret": "CONFIGURED" if self.secret_configured else "MISSING",
            "owner": self.owner,
            "enabled": self.enabled,
            "role": self.role,
        }


class ProfileRegistry:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self._profiles = self._load_profiles()

    def _load_profiles(self) -> dict[str, Profile]:
        with self.config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        profiles: dict[str, Profile] = {}
        for item in raw.get("profiles", []):
            profile = Profile(
                id=str(item["id"]),
                provider=str(item["provider"]),
                model=str(item["model"]),
                base_url=item.get("base_url") or None,
                secret_env=item.get("secret_env") or None,
                owner=str(item.get("owner", "unknown")),
                enabled=bool(item.get("enabled", False)),
                role=str(item.get("role", "worker")),
            )
            if profile.id in profiles:
                raise ValueError(f"Duplicate profile id: {profile.id}")
            profiles[profile.id] = profile
        return profiles

    def get(self, profile_id: str) -> Profile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise KeyError(f"Profile not found: {profile_id}") from exc

    def list_profiles(self) -> list[dict[str, Any]]:
        return [profile.to_safe_dict() for profile in self._profiles.values()]

    def validate_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self.get(profile_id)
        issues: list[str] = []
        if not profile.id:
            issues.append("missing id")
        if not profile.provider:
            issues.append("missing provider")
        if not profile.model:
            issues.append("missing model")
        if profile.secret_env and not profile.secret_configured:
            issues.append(f"secret env not configured: {profile.secret_env}")
        return {
            "id": profile.id,
            "valid": not issues,
            "issues": issues,
            "profile": profile.to_safe_dict(),
        }
