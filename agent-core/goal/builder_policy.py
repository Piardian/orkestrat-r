from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


DEFAULT_FORBIDDEN_PATTERNS = [
    ".env",
    "credential",
    "credentials",
    "secret",
    "token",
    "trading logic",
]

DEFAULT_ALLOWED_SAFE_COMMANDS = [
    "python -m unittest",
    "py -m unittest",
    "pytest",
    "python -m pytest",
    "npm test",
    "npm run test",
    "npm run lint",
    "npm run build",
    "npx jest",
]


@dataclass(frozen=True)
class BuilderPolicy:
    mode: str = "local-safe"
    profile: str = "gemini-user-a"
    max_runtime_seconds: int = 600
    max_iterations: int = 10_000
    max_patch_bytes: int = 200_000
    forbidden_patterns: list[str] = None  # type: ignore[assignment]
    allowed_safe_commands: list[str] = None  # type: ignore[assignment]
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 20
    rate_limit_safety_margin: int = 1
    rate_limit_window_seconds: int = 60
    rate_limit_retry_attempts: int = 3
    rate_limit_backoff_seconds: list[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.forbidden_patterns is None:
            object.__setattr__(self, "forbidden_patterns", list(DEFAULT_FORBIDDEN_PATTERNS))
        if self.allowed_safe_commands is None:
            object.__setattr__(self, "allowed_safe_commands", list(DEFAULT_ALLOWED_SAFE_COMMANDS))
        if self.rate_limit_backoff_seconds is None:
            object.__setattr__(self, "rate_limit_backoff_seconds", [5.0, 15.0, 30.0])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BuilderPolicy":
        return cls(
            mode=str(raw.get("mode", "local-safe")),
            profile=str(raw.get("profile", "gemini-user-a")),
            max_runtime_seconds=int(raw.get("max_runtime_seconds", 600) or 600),
            max_iterations=int(raw.get("max_iterations", 10_000) or 10_000),
            max_patch_bytes=int(raw.get("max_patch_bytes", 200_000) or 200_000),
            forbidden_patterns=[str(item) for item in raw.get("forbidden_patterns", DEFAULT_FORBIDDEN_PATTERNS)],
            allowed_safe_commands=[str(item) for item in raw.get("allowed_safe_commands", DEFAULT_ALLOWED_SAFE_COMMANDS)],
            rate_limit_enabled=bool(raw.get("rate_limit_enabled", True)),
            rate_limit_requests_per_minute=int(raw.get("rate_limit_requests_per_minute", 20) or 20),
            rate_limit_safety_margin=int(raw.get("rate_limit_safety_margin", 1) or 1),
            rate_limit_window_seconds=int(raw.get("rate_limit_window_seconds", 60) or 60),
            rate_limit_retry_attempts=int(raw.get("rate_limit_retry_attempts", 3) or 3),
            rate_limit_backoff_seconds=[float(item) for item in raw.get("rate_limit_backoff_seconds", [5.0, 15.0, 30.0])],
        )


def default_builder_config() -> dict[str, Any]:
    return {
        "builder": BuilderPolicy().to_dict(),
    }


def load_builder_policy(config_path: str | Path = Path("config/army.yaml")) -> BuilderPolicy:
    path = Path(config_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    raw: dict[str, Any] = {}
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            raw = loaded.get("builder", loaded.get("commander", {})) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return BuilderPolicy.from_dict(raw)
