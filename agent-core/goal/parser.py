from __future__ import annotations

import shlex


def parse_goal_command(line: str) -> str | None:
    text = (line or "").strip()
    if not text:
        return None
    if not text.startswith("/goal"):
        return None
    remainder = text[len("/goal") :].strip()
    if not remainder:
        return None
    if remainder[0] in {"'", '"'}:
        parts = shlex.split(remainder)
        if not parts:
            return None
        return parts[0].strip()
    return remainder
