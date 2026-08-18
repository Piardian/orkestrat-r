from __future__ import annotations

import argparse
import shlex
from pathlib import Path
import sys
import re

from goal import GoalMetricsService, GoalService, GoalStatusService, parse_goal_command
from goal.resume_service import GoalResumeService

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Goal intake CLI.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--goal", default=None)
    args = parser.parse_args()

    service = GoalService()
    if args.goal:
        return _handle_goal(service, args.repo, args.goal)

    while True:
        try:
            line = input("agent-core> ")
        except EOFError:
            return 0
        slash_result = _dispatch_slash_command(line)
        if slash_result is not None:
            return slash_result
        goal_text = parse_goal_command(line)
        if goal_text is None:
            print("Enter /goal \"...\" or /goals, /status, /history, /resume, /metrics")
            continue
        return _handle_goal(service, args.repo, goal_text)


def _handle_goal(service: GoalService, repo: str, goal_text: str) -> int:
    try:
        record = service.create_goal(goal_text, Path(repo))
    except ValueError as exc:
        print(f"GOAL REJECTED\n\nReason: {exc}")
        return 1

    print("GOAL CREATED\n")
    print(f"ID: {record.goal_id}")
    print(f"Status: {record.status}")
    print(f"Repo: {record.repo}")
    print(f"Goal: {_safe_goal_preview(record.goal)}")
    print("\nNext stage: PLANNING")
    return 0


def _dispatch_slash_command(line: str) -> int | None:
    text = (line or "").strip()
    if not text or not text.startswith("/"):
        return None
    parts = shlex.split(text)
    if not parts:
        return None
    command = parts[0].lower()
    service = GoalService()
    status_service = GoalStatusService(service)
    resume_service = GoalResumeService(service, runtime_root="runtime")
    metrics_service = GoalMetricsService(service, runtime_root="runtime")
    if command == "/goals":
        snapshots = status_service.list_goals(limit=10)
        print("GOALS\n")
        for item in snapshots:
            print(f"{item.goal_id} | {item.state} | {_preview(item.goal)}")
        return 0
    if command == "/status" and len(parts) >= 2:
        snap = status_service.snapshot(parts[1])
        print("GOAL STATUS\n")
        print(f"ID: {snap.goal_id}")
        print(f"State: {snap.state}")
        print(f"Next action: {snap.next_action or 'No action required'}")
        return 0
    if command == "/history" and len(parts) >= 2:
        history = status_service.history(parts[1])
        print("GOAL HISTORY\n")
        for item in history:
            print(f"{item.get('from', '')} -> {item.get('to', '')}")
        return 0
    if command == "/resume" and len(parts) >= 2:
        execute = "--execute" in parts
        result = resume_service.resume(parts[1], execute=execute)
        print("GOAL RESUME\n")
        print(f"Goal: {result.goal_id}")
        print(f"State: {result.state}")
        print(f"Action: {result.action}")
        print(f"Executed: {'YES' if result.executed else 'NO'}")
        print(f"Message: {result.message}")
        return 0
    if command == "/metrics":
        goal_id = parts[1] if len(parts) >= 2 else None
        if goal_id:
            metrics = metrics_service.refresh_goal(goal_id).to_dict()
            print("GOAL METRICS\n")
            print(f"Goal: {metrics['goal_id']}")
            print(f"State: {metrics['state']}")
            print(f"Executor: {metrics['result'].get('executor', 'unknown')}")
            print(f"Logical calls: {metrics['llm']['logical_calls']}")
            print(f"Provider requests: {metrics['llm']['provider_requests']}")
            print(f"Retries: {metrics['llm']['provider_retries']}")
            print(f"Input tokens: {metrics['llm']['input_tokens']}")
            print(f"Output tokens: {metrics['llm']['output_tokens']}")
            return 0
        totals = metrics_service.refresh_all()
        print("AGENT-CORE METRICS\n")
        print(f"Goals total: {totals['goals_total']}")
        print(f"Completed: {totals['completed']}")
        print(f"Failed: {totals['failed']}")
        print(f"LLM logical calls: {totals['llm_logical_calls']}")
        print(f"Provider requests: {totals['provider_requests']}")
        return 0
    return None


def _safe_goal_preview(text: str) -> str:
    redacted = re.sub(r"nvapi-[0-9A-Za-z_\-]+", "[REDACTED]", text)
    redacted = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED]", redacted)
    redacted = re.sub(r"sk-[0-9A-Za-z_\-]{8,}", "[REDACTED]", redacted)
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
