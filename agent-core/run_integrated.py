from __future__ import annotations

import argparse
import json
import os
import sys

from orchestration import (
    CrewAIUnavailableError,
    GoalPipelineEngine,
    PipelineRequest,
    PipelineStageError,
    run_crewai_flow,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpenClaw/CrewAI/OpenHands integrated goal runner."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--goal-id", help="Resume an existing goal.")
    source.add_argument("--task", help="Create and run a new goal.")
    parser.add_argument("--repo", help="Target git repository for a new task.")
    parser.add_argument("--runtime-dir", default="runtime/goals")
    parser.add_argument("--execution-mode", default=None)
    parser.add_argument("--commander-profile", default=None)
    parser.add_argument("--openhands-python", default=None)
    parser.add_argument(
        "--orchestrator",
        choices=["crewai", "native"],
        default=os.getenv("AGENT_ARMY_ORCHESTRATOR", "crewai"),
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="Explicitly allow the final patch to be applied to the target repo.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.task and not args.repo:
        parser.error("--repo is required with --task")

    request = PipelineRequest(
        repo=args.repo,
        task=args.task,
        goal_id=args.goal_id,
        runtime_dir=args.runtime_dir,
        execution_mode=args.execution_mode,
        commander_profile=args.commander_profile,
        openhands_python=args.openhands_python,
        auto_apply=args.auto_apply,
    )
    engine = GoalPipelineEngine(request)

    try:
        if args.orchestrator == "crewai":
            result = run_crewai_flow(engine)
        else:
            result = engine.run_native()
    except CrewAIUnavailableError as exc:
        print(f"CREWAI_UNAVAILABLE: {exc}", file=sys.stderr)
        return 4
    except PipelineStageError as exc:
        print(f"PIPELINE_FAILED stage={exc.stage}: {exc}", file=sys.stderr)
        return 5
    except Exception as exc:
        print(f"PIPELINE_FAILED: {exc}", file=sys.stderr)
        return 6

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("INTEGRATED AGENT PIPELINE")
        print(f"Goal: {result.goal_id}")
        print(f"State: {result.state}")
        print("Orchestrator:", args.orchestrator)
        print("Builder: OpenHands")
        print("OpenClaw entry: supported via openclaw/skills/agent-army")
        print(f"Applied: {'YES' if result.applied else 'NO'}")
        if result.next_action:
            print(f"Next action: {result.next_action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
