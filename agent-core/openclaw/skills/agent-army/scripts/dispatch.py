from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe OpenClaw bridge into agent-core.")
    parser.add_argument("--request-file", required=True)
    args = parser.parse_args()

    request_path = Path(args.request_file).resolve()
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Invalid request file: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, dict):
        print("Request must be a JSON object.", file=sys.stderr)
        return 2

    core_dir_raw = os.getenv("AGENT_ARMY_CORE_DIR", "").strip()
    if not core_dir_raw:
        print("AGENT_ARMY_CORE_DIR is not configured for OpenClaw.", file=sys.stderr)
        return 3
    core_dir = Path(core_dir_raw).expanduser().resolve()
    runner = core_dir / "run_integrated.py"
    if not runner.is_file():
        print(f"run_integrated.py not found under AGENT_ARMY_CORE_DIR: {core_dir}", file=sys.stderr)
        return 3

    goal_id = str(payload.get("goal_id") or "").strip()
    task = str(payload.get("task") or "").strip()
    repo = str(payload.get("repo") or "").strip()

    crewai_python_raw = os.getenv("AGENT_ARMY_CREWAI_PYTHON", "").strip()
    crewai_python = Path(crewai_python_raw).expanduser().resolve() if crewai_python_raw else Path(sys.executable).resolve()
    if not crewai_python.exists():
        print(f"CrewAI Python interpreter does not exist: {crewai_python}", file=sys.stderr)
        return 3

    command = [str(crewai_python), str(runner), "--orchestrator", "crewai", "--json"]
    if goal_id:
        command.extend(["--goal-id", goal_id])
    else:
        if not task or not repo:
            print("New requests require both 'task' and 'repo'.", file=sys.stderr)
            return 2
        repo_path = Path(repo).expanduser().resolve()
        if not repo_path.exists():
            print(f"Repo does not exist: {repo_path}", file=sys.stderr)
            return 2
        command.extend(["--task", task, "--repo", str(repo_path)])

    # Intentionally never forwards --auto-apply. OpenClaw can prepare and review
    # a patch, but applying it to the target repository remains an explicit
    # operator action outside this bridge.
    proc = subprocess.run(
        command,
        cwd=str(core_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
