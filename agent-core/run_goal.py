from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from goal import GoalPlanner, GoalService
from llm.client import LLMError
from llm.router import LLMRouter
from routing import load_commander_routing, resolve_commander_profile

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a goal planner by goal id.")
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--commander-profile", default=load_commander_routing().default_profile)
    parser.add_argument("--runtime-dir", default="runtime/goals")
    parser.add_argument("--execution-mode", default=None)
    args = parser.parse_args()

    service = GoalService(base_dir=args.runtime_dir)
    router = LLMRouter()
    planner = GoalPlanner(service=service, router=router, execution_mode=args.execution_mode)
    resolved_profile = resolve_commander_profile(args.commander_profile)
    profile = router.registry.get(resolved_profile)
    print("COMMANDER ROUTING\n")
    print(f"Commander profile: {profile.id}")
    print(f"Commander provider: {profile.provider}")
    print(f"Commander model: {profile.model}\n")

    try:
        record, search_plan, evidence_packet, impl_plan, usage = planner.plan_goal(
            args.goal_id,
            commander_profile=resolved_profile,
        )
    except (FileNotFoundError, ValueError) as exc:
        _print_failed(args.goal_id, "INVALID", str(exc))
        return 2
    except LLMError as exc:
        _print_failed(args.goal_id, exc.kind, str(exc))
        return 3

    print("GOAL PLANNED\n")
    print(f"Goal ID: {record.goal_id}")
    print(f"State: CREATED -> PLANNING -> PLANNED")
    print(f"Search planning: PASS")
    print(f"Evidence build: PASS")
    print(f"Implementation planning: PASS")
    print(f"Plan schema: PASS")
    print(f"Plan persistence: PASS")
    print(f"Logical calls: {usage['logical_calls']}")
    print(f"Provider requests: {usage['provider_requests']}")
    print(f"Provider retries: {usage['provider_retries']}")
    print(f"JSON repairs: {usage['json_repairs']}")
    print(f"Input tokens: {usage['input_tokens']}")
    print(f"Output tokens: {usage['output_tokens']}")
    print(f"Evidence size: {len(json.dumps(evidence_packet, ensure_ascii=False).encode('utf-8'))} bytes")
    print(f"Runtime storage: {service.store.goal_dir(record.goal_id)}")
    return 0


def _print_failed(goal_id: str, kind: str, message: str) -> None:
    print("GOAL PLANNING FAILED\n")
    print(f"Goal ID: {goal_id}")
    print(f"State: PLANNING_FAILED")
    print(f"Type: {kind}")
    print(f"Reason: {message}")


if __name__ == "__main__":
    raise SystemExit(main())
