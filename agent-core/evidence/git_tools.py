from __future__ import annotations

from pathlib import Path
import subprocess


def _run(repo: Path, args: list[str], max_lines: int) -> tuple[int, list[str]]:
    completed = subprocess.run(
        args,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
        shell=False,
    )
    output = (completed.stdout + completed.stderr).splitlines()
    return completed.returncode, output[:max_lines]


def git_summary(repo: Path, max_lines: int) -> dict:
    branch_code, branch_output = _run(repo, ["git", "branch", "--show-current"], max_lines)
    status_code, status_output = _run(repo, ["git", "status", "--short", "--branch"], max_lines)
    return {
        "is_repository": status_code == 0,
        "branch": branch_output[0] if branch_code == 0 and branch_output else None,
        "status": status_output,
        "status_truncated": len(status_output) >= max_lines,
    }
