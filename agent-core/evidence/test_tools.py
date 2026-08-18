from __future__ import annotations

from pathlib import Path
import subprocess

from .sanitizer import redact_text


def run_test_command(repo: Path, command: list[str], max_lines: int) -> dict:
    completed = subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=120,
        shell=False,
    )
    output = redact_text(completed.stdout + completed.stderr).splitlines()
    return {
        "command": command,
        "exit_code": completed.returncode,
        "summary": "\n".join(output[:max_lines]),
        "truncated": len(output) > max_lines,
    }
