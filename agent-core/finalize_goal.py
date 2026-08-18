from __future__ import annotations

import argparse
import sys

from goal import GoalService
from goal.finalize import FinalReviewService


def main() -> int:
    parser = argparse.ArgumentParser(description="Final review and apply gate for built goals.")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="Run deterministic final review.")
    review.add_argument("--goal-id", required=True)
    review.add_argument("--runtime-dir", default="runtime/goals")

    apply = sub.add_parser("apply", help="Apply an approved build to the target repo.")
    apply.add_argument("--goal-id", required=True)
    apply.add_argument("--runtime-dir", default="runtime/goals")
    apply.add_argument("--apply", action="store_true")

    args = parser.parse_args()
    service = GoalService(base_dir=args.runtime_dir)
    final = FinalReviewService(service=service, runtime_root="runtime")

    if args.command == "review":
        try:
            record, summary = final.review(args.goal_id)
        except Exception as exc:
            _print_error("FINAL REVIEW FAILED", args.goal_id, str(exc))
            return 2
        _print_review(record.goal_id, record.status, summary)
        return 0

    if args.command == "apply":
        try:
            record, result = final.apply(args.goal_id, explicit_apply=args.apply)
        except Exception as exc:
            _print_error("FINAL APPLY FAILED", args.goal_id, str(exc))
            return 2
        _print_apply(record.goal_id, record.status, result)
        return 0

    return 2


def _print_review(goal_id: str, state: str, summary) -> None:
    print("FINAL BUILD REVIEW\n")
    print(f"Goal: {goal_id}")
    print(f"Executor: {summary.executor}")
    print(f"Patch: {summary.changed_files}")
    print()
    print(f"Policy check: {'PASS' if summary.policy_pass else 'FAIL'}")
    print(f"Verification: {'PASS' if summary.verification_pass else 'FAIL'}")
    print()
    print("Analysts:")
    print(f"{len([item for item in summary.analysts if str(item.get('decision', '')).upper() == 'PASS'])} / {len(summary.analysts)}")
    print()
    print("Reviewer:")
    print(summary.reviewer["decision"])
    print()
    print(f"State: {state}")
    print(f"Target repo modified: NO")


def _print_apply(goal_id: str, state: str, result) -> None:
    print("FINAL APPLY RESULT\n")
    print(f"Goal: {goal_id}")
    print(f"State: {state}")
    print(f"Apply status: {result['status']}")
    print(f"Verification status: {result['verification']['status']}")
    print(f"Patch path: {result['patch_path']}")


def _print_error(title: str, goal_id: str, message: str) -> None:
    print(title)
    print()
    print(f"Goal ID: {goal_id}")
    print(f"Reason: {message}")


if __name__ == "__main__":
    raise SystemExit(main())
