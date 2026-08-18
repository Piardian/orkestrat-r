from __future__ import annotations

import argparse
import sys

from goal import GoalCodexService, GoalService


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or submit a manual Codex response for a goal.")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Generate the Codex prompt and move the goal to WAITING_CODEX.")
    prepare.add_argument("--goal-id", required=True)
    prepare.add_argument("--runtime-dir", default="runtime/goals")
    prepare.add_argument("--show", action="store_true")

    show = sub.add_parser("show", help="Display the generated Codex prompt.")
    show.add_argument("--goal-id", required=True)
    show.add_argument("--runtime-dir", default="runtime/goals")

    submit = sub.add_parser("submit", help="Submit a Codex response from stdin or a file.")
    submit.add_argument("--goal-id", required=True)
    submit.add_argument("--runtime-dir", default="runtime/goals")
    submit.add_argument("--response-file", default=None)

    args = parser.parse_args()
    service = GoalService(base_dir=args.runtime_dir)
    codex = GoalCodexService(service=service, runtime_root="runtime")

    if args.command == "prepare":
        try:
            record, request, prompt = codex.prepare(args.goal_id)
        except Exception as exc:
            _print_error("CODEX PREPARE FAILED", args.goal_id, str(exc))
            return 2
        _print_prepare(record.goal_id, request.severity, request.recommended_executor, request.prompt_path, request.handoff_path)
        if args.show:
            print()
            print(prompt)
        return 0

    if args.command == "show":
        prompt_path = service.store.goal_dir(args.goal_id) / "codex_prompt.md"
        if not prompt_path.exists():
            _print_error("CODEX PROMPT NOT FOUND", args.goal_id, str(prompt_path))
            return 2
        print(prompt_path.read_text(encoding="utf-8"))
        return 0

    if args.command == "submit":
        try:
            response_text = _load_response_text(args.response_file)
            record, artifact = codex.submit(args.goal_id, response_text)
        except Exception as exc:
            _print_error("CODEX SUBMIT FAILED", args.goal_id, str(exc))
            return 2
        _print_submit(record.goal_id, record.status, artifact)
        return 0

    return 2


def _load_response_text(path: str | None) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    return sys.stdin.read()


def _print_prepare(goal_id: str, severity: str, executor: str, prompt_path: str, handoff_path: str) -> None:
    print("CODEX REQUIRED\n")
    print(f"Goal: {goal_id}")
    print(f"Severity: {severity}")
    print(f"Recommended executor: {executor.upper()}")
    print()
    print("STEP 1")
    print(f"Copy this file to Codex:\n{prompt_path}")
    print()
    print("STEP 2")
    print(f"Paste Codex's full response back using:\npython codex_goal.py submit --goal-id {goal_id}")
    print()
    print(f"State: WAITING_CODEX")
    print(f"Codex handoff: {handoff_path}")


def _print_submit(goal_id: str, status: str, artifact) -> None:
    print("CODEX SUBMIT RESULT\n")
    print(f"Goal: {goal_id}")
    print(f"State: {status}")
    print(f"Patch status: {artifact.status}")
    print(f"Verification: {artifact.verification}")
    print(f"Build artifact: {artifact.patch_path or 'N/A'}")
    print(f"Response file: {artifact.codex_response_path}")


def _print_error(title: str, goal_id: str, message: str) -> None:
    print(title)
    print()
    print(f"Goal ID: {goal_id}")
    print(f"Reason: {message}")


if __name__ == "__main__":
    raise SystemExit(main())
