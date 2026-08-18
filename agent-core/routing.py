from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CommanderRouting:
    default_profile: str
    fallback_profile: str | None
    automatic_fallback: bool


def load_commander_routing(config_path: str | Path = Path("config") / "army.yaml") -> CommanderRouting:
    raw = _load_yaml(config_path)
    commander = raw.get("commander", {}) if isinstance(raw, dict) else {}
    default_profile = str(
        commander.get("default_profile")
        or commander.get("profile")
        or "gemini-commander-main"
    )
    fallback_profile = commander.get("fallback_profile")
    fallback_profile = str(fallback_profile) if fallback_profile else None
    automatic_fallback = bool(commander.get("automatic_fallback", False))
    return CommanderRouting(
        default_profile=default_profile,
        fallback_profile=fallback_profile,
        automatic_fallback=automatic_fallback,
    )


def resolve_commander_profile(override: str | None = None, config_path: str | Path = Path("config") / "army.yaml") -> str:
    if override and str(override).strip():
        return str(override).strip()
    return load_commander_routing(config_path).default_profile


def _load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}
