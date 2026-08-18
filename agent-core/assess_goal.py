from __future__ import annotations

import argparse

from goal import GoalComplexityService, GoalService
from llm.client import LLMError


def main() -> int:
    parser = argparse.ArgumentParser(description="Assess approved goal complexity deterministically.")
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--runtime-dir", default="runtime/goals")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    service = GoalService(base_dir=args.runtime_dir)
    complexity_service = GoalComplexityService(service=service)

    try:
        record, assessment = complexity_service.assess_goal(args.goal_id, force=args.force)
    except (FileNotFoundError, ValueError, FileExistsError) as exc:
        _print_failed(args.goal_id, "INVALID", str(exc))
        return 2
    except LLMError as exc:
        _print_failed(args.goal_id, exc.kind, str(exc))
        return 3

    print("COMPLEXITY ASSESSMENT\n")
    print(f"Goal: {record.goal_id}")
    print(f"Score: {assessment.score}")
    print(f"Severity: {assessment.severity}")
    print(f"Executor: {assessment.recommended_executor.upper()}")
    print()
    print("Factors:")
    for factor in assessment.factors:
        print(f"+{factor.score} {factor.name}: {factor.reason}")
    if assessment.hard_overrides:
        print()
        print("Hard overrides:")
        for item in assessment.hard_overrides:
            print(f"- {item}")
    print()
    print(f"Next state: {record.status}")
    print(f"complexity.json persisted: YES")
    print(f"LLM calls: {assessment.llm_calls}")
    return 0


def _print_failed(goal_id: str, kind: str, message: str) -> None:
    print("COMPLEXITY ASSESSMENT FAILED\n")
    print(f"Goal ID: {goal_id}")
    print(f"Type: {kind}")
    print(f"Reason: {message}")


if __name__ == "__main__":
    raise SystemExit(main())
