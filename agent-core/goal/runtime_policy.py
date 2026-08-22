from __future__ import annotations

import os


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _optional_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of: true, false, 1, 0, yes, no, on, off")


def mvp_unrestricted_mode() -> bool:
    """Bypass application-level scope gates only when explicitly requested."""

    explicit = _optional_bool("AGENT_ARMY_MVP_UNRESTRICTED")
    return False if explicit is None else explicit


def openhands_only_mode() -> bool:
    """Return whether complexity may inform the run but may not route away from OpenHands.

    The MVP defaults to OpenHands-only execution. The older environment names are
    accepted so existing local MVP configurations keep working instead of silently
    becoming no-ops.
    """

    explicit = _optional_bool("AGENT_ARMY_OPENHANDS_ONLY")
    if explicit is not None:
        return explicit

    force_openhands = _optional_bool("AGENT_ARMY_FORCE_OPENHANDS")
    if force_openhands is not None:
        return force_openhands

    codex_enabled = _optional_bool("AGENT_ARMY_CODEX_ENABLED")
    if codex_enabled is not None:
        return not codex_enabled

    complexity_gate_enabled = _optional_bool("AGENT_ARMY_COMPLEXITY_GATE_ENABLED")
    if complexity_gate_enabled is not None:
        return not complexity_gate_enabled

    require_codex = _optional_bool("AGENT_ARMY_REQUIRE_CODEX_FOR_COMPLEXITY")
    if require_codex is not None:
        return not require_codex

    return True


def openhands_terminal_enabled() -> bool:
    explicit = _optional_bool("AGENT_ARMY_OPENHANDS_TERMINAL_ENABLED")
    return openhands_only_mode() if explicit is None else explicit


def openhands_stuck_detection_enabled() -> bool:
    explicit = _optional_bool("AGENT_ARMY_OPENHANDS_STUCK_DETECTION")
    return not openhands_only_mode() if explicit is None else explicit


def openhands_max_iterations(default: int) -> int:
    for name in (
        "AGENT_ARMY_OPENHANDS_MAX_ITERATIONS",
        "AGENT_ARMY_BUILDER_MAX_ITERATIONS",
    ):
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value
    return max(1, int(default))
