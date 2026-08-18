from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from goal import GoalService
from goal.review_service import GoalReviewService
from llm.client import LLMError
from llm.router import LLMRouter

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run goal review over a planned goal id.")
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--reviewer-profile", default="gemini-user-d")
    parser.add_argument("--runtime-dir", default="runtime/goals")
    parser.add_argument("--execution-mode", default=None)
    args = parser.parse_args()

    service = GoalService(base_dir=args.runtime_dir)
    router = LLMRouter()
    review_service = GoalReviewService(service=service, router=router, execution_mode=args.execution_mode)

    try:
        record, analyst_results, review, goal_review, usage = review_service.review_goal(
            args.goal_id,
            reviewer_profile=args.reviewer_profile,
        )
    except (FileNotFoundError, ValueError) as exc:
        _print_failed(args.goal_id, "INVALID", str(exc))
        return 2
    except LLMError as exc:
        _print_failed(args.goal_id, exc.kind, str(exc))
        return 3

    print("GOAL REVIEWED\n")
    print(f"Goal ID: {record.goal_id}")
    print(f"State: PLANNED -> REVIEWING -> {record.status}")
    print(f"Reviewer profile: {args.reviewer_profile}")
    print("Analysts: PASS")
    print(f"Agreement: {review.agreement}")
    print(f"Final verdict: {review.final_verdict}")
    print(f"Patch required: {'YES' if review.patch_required else 'NO'}")
    print(f"Logical calls: {goal_review.logical_calls}")
    print(f"Provider requests: {goal_review.provider_requests}")
    print(f"Provider retries: {goal_review.provider_retries}")
    print(f"JSON repairs: {goal_review.json_repairs}")
    print(f"Stage regenerations: {goal_review.stage_regenerations}")
    print(f"Input tokens: {goal_review.input_tokens}")
    print(f"Output tokens: {goal_review.output_tokens}")
    print(f"Runtime storage: {service.store.goal_dir(record.goal_id)}")
    return 0


def _print_failed(goal_id: str, kind: str, message: str) -> None:
    print("GOAL REVIEW FAILED\n")
    print(f"Goal ID: {goal_id}")
    print(f"State: REVIEW_FAILED")
    print(f"Type: {kind}")
    print(f"Reason: {message}")


if __name__ == "__main__":
    raise SystemExit(main())
