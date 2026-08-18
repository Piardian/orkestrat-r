from __future__ import annotations

from pathlib import Path


MAX_PLAN_TEXT_LENGTH = 4000


def validate_goal_text(goal: str) -> str:
    text = (goal or "").strip()
    if not text:
        raise ValueError("Goal cannot be empty.")
    return text


def validate_repo_path(repo: str | Path) -> Path:
    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        raise ValueError("Repo path must exist and be a directory.")
    return repo_path


def validate_plan_text(value: str, field_name: str = "plan") -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty.")
    if len(text) > MAX_PLAN_TEXT_LENGTH:
        raise ValueError(f"{field_name} is too long.")
    return text
