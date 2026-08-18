from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .finalize import FinalReviewService as BaseFinalReviewService
from .metrics_service import GoalMetricsService
from .verification_sandbox import run_docker_verification_suite


class SandboxedFinalReviewService(BaseFinalReviewService):
    """Final review with sandboxed verification and transactional Git promotion.

    A patch is never first tested by mutating the user's checked-out repository.
    Apply creates a detached Git worktree from the approved base commit, applies and
    verifies the patch there, commits the verified tree, and only then fast-forwards
    the real repository. The staged commit SHA is persisted before promotion so an
    APPLYING activity can safely resume after a crash. If post-promotion verification
    fails, Git resets the repository to the approved base commit while the repo lock
    is still held.
    """

    def _run_verification(self, workspace: Path, commands: list[str]) -> dict[str, Any]:
        mode = os.getenv("AGENT_ARMY_VERIFICATION_SANDBOX", "host").strip().lower()
        if mode == "docker":
            return run_docker_verification_suite(
                commands,
                workspace,
                timeout=float(os.getenv("AGENT_ARMY_FINAL_VERIFY_TIMEOUT_SECONDS", "300")),
                image=(
                    os.getenv("AGENT_ARMY_VERIFICATION_IMAGE", "").strip()
                    or "ghcr.io/openhands/agent-server:latest-python"
                ),
            )
        if mode not in {"host", "local"}:
            raise ValueError(f"Unsupported AGENT_ARMY_VERIFICATION_SANDBOX: {mode}")
        return super()._run_verification(workspace, commands)

    def apply(self, goal_id: str, explicit_apply: bool) -> tuple[Any, dict[str, Any]]:
        if not explicit_apply:
            raise ValueError("EXPLICIT_APPLY_REQUIRED")

        record = self.service.read_goal(goal_id)
        state = record.status.strip().upper()
        if state not in {"READY_TO_APPLY", "APPLYING"}:
            raise ValueError("Goal must be READY_TO_APPLY or APPLYING before apply/resume.")

        goal_dir = self.service.store.goal_dir(goal_id)
        manifest = self._load_json(goal_dir / "apply_manifest.json")
        request = self._load_request(goal_dir)
        patch_text = self._load_patch(goal_dir)
        patch_sha256 = self._sha256_text(patch_text)
        if patch_sha256 != str(manifest.get("patch_sha256", "")):
            raise ValueError("PATCH_HASH_MISMATCH")

        repo = Path(request.get("target_repo", ""))
        if not self._is_git_repo(repo):
            failed = self._transition_apply_failure(record, "APPLY_FAILED", "target repo is not a git repo")
            return failed, {"status": "FAIL", "reason": "NOT_GIT"}

        verify_cmds = list(request.get("verification_commands", []))
        private_cmds = request.get("private_verification_commands") or request.get("hidden_verification_commands") or []
        all_verify_cmds = verify_cmds + [c for c in private_cmds if c not in verify_cmds]
        base_commit = str(manifest.get("base_commit", "")).strip()
        patch_path = goal_dir / "build.patch"
        transaction_path = goal_dir / "apply_transaction.json"

        with self._acquire_repo_lock(repo):
            # Re-read after the repository lock; a Temporal retry may have resumed
            # an APPLYING transaction that another process already advanced.
            record = self.service.read_goal(goal_id)
            state = record.status.strip().upper()
            if state == "READY_TO_APPLY":
                applying = self.service.update_status(record, "APPLYING", phase="applying", note="transactional apply started")
            elif state == "APPLYING":
                applying = record
            elif state == "COMPLETED":
                verification = self._run_verification(repo, all_verify_cmds)
                return applying if False else record, self._apply_result(record, patch_path, verification, status="PASS")
            else:
                raise ValueError(f"Goal cannot resume apply from state {state}")

            if not self._is_git_clean(repo):
                failed = self.service.update_status(applying, "APPLY_FAILED", phase="apply-failed", note="target repo dirty")
                return failed, {"status": "FAIL", "reason": "DIRTY_REPO"}

            head = self._git(repo, "rev-parse", "HEAD").strip()
            txn = self._load_optional_json(transaction_path)
            staged_commit = str(txn.get("staged_commit", "")).strip() if txn else ""
            staged_valid = bool(staged_commit and self._commit_exists(repo, staged_commit))

            # A crash after the verified commit was promoted but before COMPLETED is
            # safe: HEAD identifies exactly which previously verified transaction won.
            if staged_valid and head == staged_commit:
                return self._finish_promoted_apply(
                    applying,
                    repo,
                    patch_path,
                    transaction_path,
                    txn,
                    base_commit,
                    staged_commit,
                    all_verify_cmds,
                )

            if head != base_commit:
                failed = self.service.update_status(applying, "APPLY_FAILED", phase="apply-failed", note="base commit changed")
                return failed, {"status": "FAIL", "reason": "BASE_CHANGED", "head": head, "expected": base_commit}

            if not staged_valid:
                staged_commit, staged_verification = self._stage_verified_commit(
                    goal_id,
                    repo,
                    base_commit,
                    patch_path,
                    patch_sha256,
                    all_verify_cmds,
                )
                if not staged_commit:
                    failed = self.service.update_status(
                        applying,
                        "POST_APPLY_VERIFICATION_FAILED",
                        phase="post-apply-verification-failed",
                        note="verification failed in isolated apply worktree; real repo unchanged",
                    )
                    summary = self._apply_result(failed, patch_path, staged_verification, status="FAIL")
                    self._persist_final_verification(goal_dir, summary)
                    GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(failed.goal_id)
                    return failed, summary

                txn = {
                    "goal_id": goal_id,
                    "base_commit": base_commit,
                    "staged_commit": staged_commit,
                    "patch_sha256": patch_sha256,
                    "status": "VERIFIED",
                }
                self.service.store.save_plan(goal_id, "apply_transaction.json", txn)

            promote = subprocess.run(
                ["git", "-C", str(repo), "merge", "--ff-only", staged_commit],
                capture_output=True,
                text=True,
                check=False,
            )
            if promote.returncode != 0:
                failed = self.service.update_status(
                    applying,
                    "APPLY_FAILED",
                    phase="apply-failed",
                    note="verified commit could not be fast-forwarded into target repo",
                )
                summary = {
                    "status": "FAIL",
                    "reason": "FAST_FORWARD_FAILED",
                    "stdout": promote.stdout,
                    "stderr": promote.stderr,
                    "staged_commit": staged_commit,
                }
                self._persist_final_verification(goal_dir, summary)
                return failed, summary

            txn = dict(txn or {})
            txn.update({"status": "PROMOTED", "staged_commit": staged_commit, "base_commit": base_commit})
            self.service.store.save_plan(goal_id, "apply_transaction.json", txn)
            return self._finish_promoted_apply(
                applying,
                repo,
                patch_path,
                transaction_path,
                txn,
                base_commit,
                staged_commit,
                all_verify_cmds,
            )

    def _stage_verified_commit(
        self,
        goal_id: str,
        repo: Path,
        base_commit: str,
        patch_path: Path,
        patch_sha256: str,
        commands: list[str],
    ) -> tuple[str | None, dict[str, Any]]:
        worktree = Path(tempfile.gettempdir()) / f"agent-army-apply-{goal_id}-{os.getpid()}"
        self._cleanup_apply_worktree(repo, worktree)
        add = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), base_commit],
            capture_output=True,
            text=True,
            check=False,
        )
        if add.returncode != 0:
            return None, {
                "status": "FAIL",
                "exit_code": add.returncode,
                "command": "git worktree add",
                "stdout": add.stdout,
                "stderr": add.stderr,
                "failure_code": "APPLY_WORKTREE_CREATE_FAILED",
                "command_results": [],
            }

        try:
            applied = self._git_apply(worktree, patch_path, check_only=False)
            if applied.returncode != 0:
                return None, {
                    "status": "FAIL",
                    "exit_code": applied.returncode,
                    "command": "git apply",
                    "stdout": applied.stdout,
                    "stderr": applied.stderr,
                    "failure_code": "APPLY_PATCH_FAILED",
                    "command_results": [],
                }

            verification = self._run_verification(worktree, commands)
            if verification.get("status") != "PASS":
                return None, verification

            for key, value in (("user.email", "agent-army@localhost"), ("user.name", "Agent Army")):
                subprocess.run(
                    ["git", "-C", str(worktree), "config", key, value],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            subprocess.run(["git", "-C", str(worktree), "add", "-A"], capture_output=True, text=True, check=True)
            committed = subprocess.run(
                ["git", "-C", str(worktree), "commit", "-m", f"Agent Army verified apply {goal_id}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if committed.returncode != 0:
                return None, {
                    "status": "FAIL",
                    "exit_code": committed.returncode,
                    "command": "git commit",
                    "stdout": committed.stdout,
                    "stderr": committed.stderr,
                    "failure_code": "APPLY_COMMIT_FAILED",
                    "command_results": [],
                }
            staged_commit = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            return staged_commit, verification
        finally:
            self._cleanup_apply_worktree(repo, worktree)

    def _finish_promoted_apply(
        self,
        applying: Any,
        repo: Path,
        patch_path: Path,
        transaction_path: Path,
        txn: dict[str, Any],
        base_commit: str,
        staged_commit: str,
        commands: list[str],
    ) -> tuple[Any, dict[str, Any]]:
        verification = self._run_verification(repo, commands)
        goal_dir = self.service.store.goal_dir(applying.goal_id)
        if verification.get("status") != "PASS":
            rollback = subprocess.run(
                ["git", "-C", str(repo), "reset", "--hard", base_commit],
                capture_output=True,
                text=True,
                check=False,
            )
            if rollback.returncode != 0:
                failed = self.service.update_status(
                    applying,
                    "APPLY_FAILED",
                    phase="apply-failed",
                    note="post-promotion verification failed and automatic rollback failed",
                )
                summary = self._apply_result(failed, patch_path, verification, status="FAIL")
                summary.update(
                    {
                        "reason": "ROLLBACK_FAILED",
                        "rollback_stdout": rollback.stdout,
                        "rollback_stderr": rollback.stderr,
                        "staged_commit": staged_commit,
                    }
                )
                self._persist_final_verification(goal_dir, summary)
                return failed, summary

            txn = dict(txn)
            txn.update({"status": "ROLLED_BACK", "rollback_to": base_commit})
            self.service.store.save_plan(applying.goal_id, "apply_transaction.json", txn)
            failed = self.service.update_status(
                applying,
                "POST_APPLY_VERIFICATION_FAILED",
                phase="post-apply-verification-failed",
                note="post-promotion verification failed; Git rollback restored approved base commit",
            )
            summary = self._apply_result(failed, patch_path, verification, status="FAIL")
            summary.update({"reason": "POST_APPLY_VERIFICATION_FAILED_ROLLED_BACK", "rolled_back": True})
            self._persist_final_verification(goal_dir, summary)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(failed.goal_id)
            return failed, summary

        txn = dict(txn)
        txn.update({"status": "COMPLETED", "promoted_commit": staged_commit})
        self.service.store.save_plan(applying.goal_id, "apply_transaction.json", txn)
        completed = self.service.update_status(applying, "COMPLETED", phase="completed", note="transactional apply complete")
        summary = self._apply_result(completed, patch_path, verification, status="PASS")
        summary.update({"transactional": True, "promoted_commit": staged_commit})
        self._persist_final_verification(goal_dir, summary)
        GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(completed.goal_id)
        return completed, summary

    def _transition_apply_failure(self, record: Any, status: str, note: str) -> Any:
        state = record.status.strip().upper()
        if state == "READY_TO_APPLY":
            record = self.service.update_status(record, "APPLYING", phase="applying", note="apply preflight")
        return self.service.update_status(record, status, phase="apply-failed", note=note)

    def _commit_exists(self, repo: Path, commit: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def _cleanup_apply_worktree(self, repo: Path, worktree: Path) -> None:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(worktree, ignore_errors=True)
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "prune"],
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _load_optional_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}
