from __future__ import annotations

from pathlib import Path

from .sanitizer import is_secret_path, redact_text


def read_line_range(
    repo: Path,
    relative_path: str,
    line_start: int,
    line_end: int,
    max_chars: int = 12000,
) -> dict:
    if is_secret_path(relative_path):
        return {
            "path": relative_path,
            "line_start": line_start,
            "line_end": line_end,
            "content": "[REDACTED_SECRET_FILE]",
            "truncated": False,
        }

    target = (repo / relative_path).resolve()
    repo_root = repo.resolve()
    if repo_root not in target.parents and target != repo_root:
        raise ValueError(f"Path escapes repository: {relative_path}")

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start_index = max(line_start - 1, 0)
    end_index = min(line_end, len(lines))
    content = "\n".join(lines[start_index:end_index])
    truncated_by_chars = len(content) > max_chars
    if truncated_by_chars:
        content = content[:max_chars] + "\n[TRUNCATED_BY_CHAR_LIMIT]"
    return {
        "path": relative_path,
        "line_start": line_start,
        "line_end": end_index,
        "content": redact_text(content),
        "truncated": end_index < len(lines) or truncated_by_chars,
    }
