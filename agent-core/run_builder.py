from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess

from goal import GoalBuilderService, GoalService
from goal.builder_policy import load_builder_policy


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic builder for approved goals.")
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--runtime-dir", default="runtime/goals")
    parser.add_argument("--mode", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    service = GoalService(base_dir=args.runtime_dir)
    policy = load_builder_policy()
    if args.mode:
        policy = replace(policy, mode=args.mode)

    workspace_mode = os.getenv("AGENT_ARMY_OPENHANDS_WORKSPACE", "docker").strip().lower()
    adapter = None
    if workspace_mode == "docker":
        # OpenHands' maintained DockerWorkspace examples use the latest-python
        # agent-server tag unless CI supplies a commit-specific image.
        os.environ.setdefault("AGENT_ARMY_OPENHANDS_SERVER_IMAGE", "ghcr.io/openhands/agent-server:latest-python")
        os.environ.setdefault("AGENT_ARMY_VERIFICATION_IMAGE", "ghcr.io/openhands/agent-server:latest-python")
        from goal.openhands_docker_hardened import HardenedDockerOpenHandsBuilderAdapter

        adapter = HardenedDockerOpenHandsBuilderAdapter(policy)
    elif workspace_mode != "local":
        print(f"Unsupported AGENT_ARMY_OPENHANDS_WORKSPACE: {workspace_mode}")
        return 2

    builder = GoalBuilderService(
        service=service,
        execution_mode=args.mode,
        policy=policy,
        adapter=adapter,
    )

    if args.dry_run and args.execute:
        print("Choose exactly one of --dry-run or --execute.")
        return 2

    try:
        if args.dry_run or not args.execute:
            record, request = builder.dry_run(args.goal_id)
            _print_dry_run(record.goal_id, request, workspace_mode)
            return 0

        record, request, result = builder.execute(args.goal_id)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print("OPENHANDS BUILDER FAILED\n")
        print(f"Goal ID: {args.goal_id}")
        print(f"Reason: {exc}")
        return 2
    except Exception as exc:
        print("OPENHANDS BUILDER FAILED\n")
        print(f"Goal ID: {args.goal_id}")
        print(f"Reason: {exc}")
        return 3

    cleanup_status = _cleanup_staging_worktree(request)

    print("OPENHANDS BUILDER\n")
    print(f"Goal: {record.goal_id}")
    print(f"State: {record.phase}")
    print(f"Mode: {request.mode}")
    print(f"Workspace isolation: {workspace_mode}")
    print(f"Builder profile: {request.builder_profile}")
    print()
    print("Allowed files:")
    for item in request.allowed_files:
        print(f"- {item}")
    print()
    print(f"Workspace: {request.workspace_path}")
    print(f"Workspace cleanup: {cleanup_status}")
    print(f"OpenHands: {'SUCCESS' if result.openhands_executed else 'NOT_EXECUTED'}")
    print("Changed files:")
    for item in result.changed_files:
        print(f"- {item}")
    print()
    print(f"Provider requests: {result.provider_requests}")
    print(f"Provider retries: {result.provider_retries}")
    print(f"Builder rate-limit waits: {result.builder_rate_limit_waits}")
    print(f"Builder rate-limit wait seconds: {result.builder_rate_limit_wait_seconds:.1f}")
    print(f"429 count: {result.provider_429_count}")
    print(f"503 count: {result.provider_503_count}")
    print(f"Timeout count: {result.provider_timeout_count}")
    print(f"Quota exhausted: {result.quota_exhausted_count}")
    if result.preflight_warnings:
        print("Preflight warnings:")
        for warning in result.preflight_warnings:
            print(f"- {json.dumps(warning, ensure_ascii=False)}")
    print(f"Retry exhausted: {'YES' if result.retry_exhausted else 'NO'}")
    print()
    print(f"Verification: {result.verification_status or 'N/A'}")
    if result.verification_result and result.verification_result.get("sandbox"):
        print(f"Verification sandbox: {result.verification_result['sandbox']}")
    print(f"Original repo modified: {'YES' if result.original_repo_modified else 'NO'}")
    print(f"Patch: {result.patch_path or 'N/A'}")
    print(f"NEXT: {record.status}")
    return 0


def _cleanup_staging_worktree(request) -> str:
    if _truthy(os.getenv("AGENT_ARMY_KEEP_WORKSPACE")):
        return "KEPT"
    repo = Path(request.target_repo)
    workspace = Path(request.workspace_path)
    if not repo.exists() or not workspace.exists():
        return "NOT_PRESENT"
    try:
        remove = subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(workspace)],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "prune"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "REMOVED" if remove.returncode == 0 else "PRUNE_REQUIRED"
    except Exception:
        return "CLEANUP_FAILED"


def _print_dry_run(goal_id: str, request, workspace_mode: str) -> None:
    print("OPENHANDS BUILDER DRY-RUN\n")
    print(f"Goal: {goal_id}")
    print("OpenHands executed: NO")
    print(f"Workspace isolation: {workspace_mode}")
    print("Allowed files:")
    for item in request.allowed_files:
        print(f"- {item}")
    print()
    print("Verification commands:")
    for item in request.verification_commands:
        print(f"- {item}")
    print()
    print("Request:")
    print(json.dumps(request.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
