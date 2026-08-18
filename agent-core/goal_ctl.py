from __future__ import annotations

import argparse
from pathlib import Path

import sys

from goal import GoalMetricsService, GoalService
from goal.resume_service import GoalResumeService
from goal.status_service import GoalStatusService

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Goal status, history and safe resume CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    goals = sub.add_parser("goals", help="List recent goals.")
    goals.add_argument("--runtime-dir", default="runtime/goals")
    goals.add_argument("--status", default=None)
    goals.add_argument("--limit", type=int, default=20)

    status = sub.add_parser("status", help="Show a goal status snapshot.")
    status.add_argument("--goal-id", required=True)
    status.add_argument("--runtime-dir", default="runtime/goals")

    history = sub.add_parser("history", help="Show a goal transition history.")
    history.add_argument("--goal-id", required=True)
    history.add_argument("--runtime-dir", default="runtime/goals")

    resume = sub.add_parser("resume", help="Resume a goal safely.")
    resume.add_argument("--goal-id", required=True)
    resume.add_argument("--runtime-dir", default="runtime/goals")
    resume.add_argument("--execute", action="store_true")

    metrics = sub.add_parser("metrics", help="Show goal or global metrics.")
    metrics.add_argument("--goal-id", default=None)
    metrics.add_argument("--runtime-dir", default="runtime/goals")

    args = parser.parse_args()
    service = GoalService(base_dir=args.runtime_dir)
    status_service = GoalStatusService(service)
    resume_service = GoalResumeService(service, runtime_root="runtime")
    metrics_service = GoalMetricsService(service, runtime_root="runtime")

    if args.command == "goals":
        return _cmd_goals(status_service, args.status, args.limit)
    if args.command == "status":
        return _cmd_status(status_service, args.goal_id)
    if args.command == "history":
        return _cmd_history(status_service, args.goal_id)
    if args.command == "resume":
        return _cmd_resume(resume_service, args.goal_id, args.execute)
    if args.command == "metrics":
        return _cmd_metrics(metrics_service, args.goal_id)
    return 2


def _cmd_goals(status_service: GoalStatusService, status: str | None, limit: int) -> int:
    snapshots = status_service.list_goals(status=status, limit=limit)
    print("GOALS\n")
    for item in snapshots:
        print(f"{item.goal_id}")
        print(f"Status: {item.state}")
        print(f"Goal: {_preview(item.goal)}")
        print(f"Updated: {item.updated_at}")
        print()
    return 0


def _cmd_status(status_service: GoalStatusService, goal_id: str) -> int:
    snap = status_service.snapshot(goal_id)
    print("GOAL STATUS\n")
    print(f"ID: {snap.goal_id}")
    print(f"Type: {snap.goal_type}")
    print(f"Goal: {_preview(snap.goal)}")
    print(f"State: {snap.state}")
    print(f"Repo: {snap.repo}")
    print()
    print("Completed:")
    for item in snap.completed:
        print(f"✓ {item}")
    print()
    print("Current:")
    print(f"→ {snap.current}")
    print()
    print("Remaining:")
    for item in snap.remaining:
        print(f"□ {item}")
    print()
    print("Next action:")
    print(snap.next_action or "No action required")
    return 0


def _cmd_history(status_service: GoalStatusService, goal_id: str) -> int:
    history = status_service.history(goal_id)
    print("GOAL HISTORY\n")
    for item in history:
        print(f"{item.get('from', '')}")
        print(f"↓ {item.get('to', '')}")
    return 0


def _cmd_resume(resume_service: GoalResumeService, goal_id: str, execute: bool) -> int:
    result = resume_service.resume(goal_id, execute=execute)
    print("GOAL RESUME\n")
    print(f"Goal: {result.goal_id}")
    print(f"State: {result.state}")
    print(f"Action: {result.action}")
    print(f"Executed: {'YES' if result.executed else 'NO'}")
    print(f"Lock: {'ACQUIRED' if result.lock_acquired else 'NOT ACQUIRED'}")
    print(f"Message: {result.message}")
    return 0


def _cmd_metrics(metrics_service: GoalMetricsService, goal_id: str | None) -> int:
    if goal_id:
        metrics = metrics_service.refresh_goal(goal_id).to_dict()
        print("GOAL METRICS\n")
        print(f"Goal: {metrics['goal_id']}")
        print(f"State: {metrics['state']}")
        print(f"Executor: {metrics['result'].get('executor', 'unknown')}")
        print()
        print("LLM")
        print(f"Logical calls: {metrics['llm']['logical_calls']}")
        print(f"Provider requests: {metrics['llm']['provider_requests']}")
        print(f"Retries: {metrics['llm']['provider_retries']}")
        print(f"JSON repairs: {metrics['llm']['json_repairs']}")
        print(f"Input tokens: {metrics['llm']['input_tokens']}")
        print(f"Output tokens: {metrics['llm']['output_tokens']}")
        print()
        print("BUILDER")
        print(f"Attempts: {metrics['builder'].get('attempts', 0)}")
        print(f"Changed files: {metrics['builder'].get('changed_file_count', 0)}")
        print(f"Verification: {metrics['verification'].get('status', 'UNKNOWN')}")
        print()
        print("COMPLEXITY")
        print(f"Score: {metrics['complexity'].get('score', 0)}")
        print(f"Severity: {metrics['complexity'].get('severity', 'UNKNOWN')}")
        print()
        print("PROVIDER HEALTH")
        print(f"503: {metrics['providers'].get('503', 0)}")
        print(f"429: {metrics['providers'].get('429', 0)}")
        print()
        print("Estimated cost:")
        print(metrics["estimated_cost"])
        return 0

    totals = metrics_service.refresh_all()
    print("AGENT-CORE METRICS\n")
    print(f"Goals total: {totals['goals_total']}")
    print(f"Completed: {totals['completed']}")
    print(f"Failed: {totals['failed']}")
    print(f"Waiting manual action: {totals['waiting_manual_action']}")
    print()
    print(f"OpenHands builds: {totals['openhands_builds']}")
    print(f"Codex builds: {totals['codex_builds']}")
    print()
    print(f"LLM logical calls: {totals['llm_logical_calls']}")
    print(f"Provider requests: {totals['provider_requests']}")
    print(f"Retries: {totals['retries']}")
    print()
    print(f"429: {totals['429']}")
    print(f"503: {totals['503']}")
    print()
    print(f"Total input tokens: {totals['total_input_tokens']}")
    print(f"Total output tokens: {totals['total_output_tokens']}")
    return 0


def _preview(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text[: limit - 3] + "..." if len(text) > limit else text


if __name__ == "__main__":
    raise SystemExit(main())
