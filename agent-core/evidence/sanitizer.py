from __future__ import annotations

import re


SECRET_FILE_PATTERNS = (
    ".env",
    ".env.",
    ".pem",
    ".key",
    "credentials",
    "secrets",
)

SECRET_VALUE_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"sk-[0-9A-Za-z_\-]{20,}"),
    re.compile(r"nvapi-[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|authorization)(\s*[=:]\s*)([^\s\"']+)"),
]


def is_secret_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    return any(pattern in name for pattern in SECRET_FILE_PATTERNS)


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_VALUE_PATTERNS[:3]:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = SECRET_VALUE_PATTERNS[3].sub(r"\1\2[REDACTED]", redacted)
    return redacted
