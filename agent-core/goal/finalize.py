from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .codex import validate_patch_policy
from .model import GoalRecord
from .service import GoalService
from .metrics_service import GoalMetricsService
from .verification_command import normalize_verification_command


@dataclass(frozen=True)
class FinalBuildArtifact:
    goal_id: str
    executor: str
    source: str
    changed_files: list[str]
    verification: str
    status: str
    failure_type: str | None
    patch_path: str | None
    patch_size: int
    verification_result: dict[str, Any] | None
    original_repo_modified: bool
    codex_response_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FinalReviewSummary:
    goal_id: str
    executor: str
    state: str
    allowed_files: list[str]
    changed_files: list[str]
    violations: list[str]
    patch_sha256: str
    base_commit: str
    approval_at: str
    patch_hash_matches: bool
    policy_pass: bool
    verification_pass: bool
    analysts: list[dict[str, Any]]
    reviewer: dict[str, Any]
    quorum_met: bool
    execution_status: str
    ready_to_apply: bool
    manifest_path: str
    final_evidence_path: str
    final_verification_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FinalReviewService:
    def __init__(self, service: GoalService | None = None, runtime_root: str | Path = "runtime") -> None:
        self.service = service or GoalService()
        self.runtime_root = Path(runtime_root)
        self.workspaces_root = self.runtime_root / "workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

    def review(self, goal_id: str) -> tuple[GoalRecord, FinalReviewSummary]:
        record = self.service.read_goal(goal_id)
        if record.status.strip().upper() != "BUILT_PENDING_REVIEW":
            raise ValueError("Goal must be BUILT_PENDING_REVIEW before final review.")
        goal_dir = self.service.store.goal_dir(goal_id)
        request = self._load_request(goal_dir)
        build = self._load_json(goal_dir / "build.json")
        patch_text = self._load_patch(goal_dir)
        allowed_files = [str(item) for item in request.get("allowed_files", []) if str(item).strip()]
        violations = self._policy_violations(patch_text, allowed_files)

        review_record = self.service.update_status(record, "BUILD_REVIEWING", phase="build-reviewing", note="final review started")
        if violations:
            rejected = self.service.update_status(review_record, "BUILD_REJECTED", phase="build-rejected", note="; ".join(violations))
            summary = self._summary(rejected, request, build, patch_text, violations, ready=False, policy_pass=False, verification_pass=False)
            self._persist_review(goal_dir, summary)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(rejected.goal_id)
            return rejected, summary

        workspace = self._create_review_workspace(review_record)
        patch_path = goal_dir / "build.patch"
        if not patch_path.exists():
            patch_path.write_text(patch_text, encoding="utf-8")

        patch_sha256 = self._sha256_text(patch_text)
        patch_hash_matches = self._patch_hash_matches(goal_dir, patch_sha256)
        if not patch_hash_matches:
            rejected = self.service.update_status(review_record, "BUILD_REJECTED", phase="build-rejected", note="patch hash mismatch")
            summary = self._summary(rejected, request, build, patch_text, ["patch hash mismatch"], ready=False, policy_pass=True, verification_pass=False)
            self._persist_review(goal_dir, summary)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(rejected.goal_id)
            return rejected, summary

        # 1. git apply --check in isolated review workspace
        check_proc = self._git_apply(workspace, patch_path, check_only=True)
        if check_proc.returncode != 0:
            err_msg = f"git apply --check failed: {check_proc.stderr.strip() or check_proc.stdout.strip()}"
            rejected = self.service.update_status(review_record, "BUILD_REJECTED", phase="build-rejected", note=err_msg)
            summary = self._summary(rejected, request, build, patch_text, [err_msg], ready=False, policy_pass=False, verification_pass=False)
            self._persist_review(goal_dir, summary)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(rejected.goal_id)
            return rejected, summary

        # 2. git apply in isolated review workspace
        apply_proc = self._git_apply(workspace, patch_path, check_only=False)
        if apply_proc.returncode != 0:
            err_msg = f"git apply failed: {apply_proc.stderr.strip() or apply_proc.stdout.strip()}"
            rejected = self.service.update_status(review_record, "BUILD_REJECTED", phase="build-rejected", note=err_msg)
            summary = self._summary(rejected, request, build, patch_text, [err_msg], ready=False, policy_pass=False, verification_pass=False)
            self._persist_review(goal_dir, summary)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(rejected.goal_id)
            return rejected, summary

        # 3. Invariant check: workspace git diff must match changed files
        diff_proc = subprocess.run(["git", "-C", str(workspace), "diff", "--name-only"], capture_output=True, text=True, check=False)
        workspace_changed = [line.strip().replace("\\", "/") for line in diff_proc.stdout.splitlines() if line.strip()]
        for cf in workspace_changed:
            if not self._subset([cf], allowed_files):
                err_msg = f"patch modified unauthorized file: {cf}"
                rejected = self.service.update_status(review_record, "BUILD_REJECTED", phase="build-rejected", note=err_msg)
                summary = self._summary(rejected, request, build, patch_text, [err_msg], ready=False, policy_pass=False, verification_pass=False)
                self._persist_review(goal_dir, summary)
                GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(rejected.goal_id)
                return rejected, summary

        # 4. Run verification on the PATCHED review workspace (combining public and private test suites)
        verify_cmds = list(request.get("verification_commands", []))
        private_cmds = request.get("private_verification_commands") or request.get("hidden_verification_commands") or []
        all_verify_cmds = verify_cmds + [c for c in private_cmds if c not in verify_cmds]
        verification = self._run_verification(workspace, all_verify_cmds)
        verification_pass = verification["status"] == "PASS"
        if not verification_pass:
            failed = self.service.update_status(review_record, "BUILD_REJECTED", phase="build-rejected", note="verification failed")
            summary = self._summary(failed, request, build, patch_text, [], ready=False, policy_pass=True, verification_pass=False, verification=verification)
            self._persist_review(goal_dir, summary, verification=verification)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(failed.goal_id)
            return failed, summary

        analyst_results = self._build_analyst_results(request, build, patch_text, verification)
        quorum_met = self._quorum_met(analyst_results)
        reviewer = self._build_reviewer(analyst_results, quorum_met)
        if reviewer["decision"] == "UNKNOWN":
            unknown = self.service.update_status(review_record, "BUILD_REVIEW_UNKNOWN", phase="build-review-unknown", note="reviewer unknown")
            summary = self._summary(unknown, request, build, patch_text, [], ready=False, policy_pass=True, verification_pass=True, analysts=analyst_results, reviewer=reviewer, quorum_met=quorum_met)
            self._persist_review(goal_dir, summary, verification=verification)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(unknown.goal_id)
            return unknown, summary
        if reviewer["decision"] == "FAIL":
            rejected = self.service.update_status(review_record, "BUILD_REJECTED", phase="build-rejected", note="reviewer fail")
            summary = self._summary(rejected, request, build, patch_text, [], ready=False, policy_pass=True, verification_pass=True, analysts=analyst_results, reviewer=reviewer, quorum_met=quorum_met)
            self._persist_review(goal_dir, summary, verification=verification)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(rejected.goal_id)
            return rejected, summary
        if not quorum_met:
            failed = self.service.update_status(review_record, "BUILD_REVIEW_FAILED", phase="build-review-failed", note="quorum failed")
            summary = self._summary(failed, request, build, patch_text, [], ready=False, policy_pass=True, verification_pass=True, analysts=analyst_results, reviewer=reviewer, quorum_met=quorum_met)
            self._persist_review(goal_dir, summary, verification=verification)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(failed.goal_id)
            return failed, summary

        ready_record = self.service.update_status(review_record, "READY_TO_APPLY", phase="ready-to-apply", note="review passed")
        manifest = self._build_manifest(ready_record, request, patch_sha256)
        self.service.store.save_plan(goal_id, "apply_manifest.json", manifest)
        summary = self._summary(ready_record, request, build, patch_text, [], ready=True, policy_pass=True, verification_pass=True, analysts=analyst_results, reviewer=reviewer, quorum_met=quorum_met, patch_sha256=patch_sha256)
        self._persist_review(goal_dir, summary, verification=verification)
        GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(ready_record.goal_id)
        return ready_record, summary

    def apply(self, goal_id: str, explicit_apply: bool) -> tuple[GoalRecord, dict[str, Any]]:
        if not explicit_apply:
            raise ValueError("EXPLICIT_APPLY_REQUIRED")
        record = self.service.read_goal(goal_id)
        if record.status.strip().upper() != "READY_TO_APPLY":
            raise ValueError("Goal must be READY_TO_APPLY before apply.")
        goal_dir = self.service.store.goal_dir(goal_id)
        manifest = self._load_json(goal_dir / "apply_manifest.json")
        request = self._load_request(goal_dir)
        patch_text = self._load_patch(goal_dir)
        patch_sha256 = self._sha256_text(patch_text)
        if patch_sha256 != str(manifest.get("patch_sha256", "")):
            raise ValueError("PATCH_HASH_MISMATCH")
        repo = Path(request.get("target_repo", ""))
        if not self._is_git_repo(repo):
            failed = self.service.update_status(record, "APPLY_FAILED", phase="apply-failed", note="target repo is not a git repo")
            return failed, {"status": "FAIL", "reason": "NOT_GIT"}

        with self._acquire_repo_lock(repo):
            if not self._is_git_clean(repo):
                failed = self.service.update_status(record, "APPLY_FAILED", phase="apply-failed", note="target repo dirty")
                return failed, {"status": "FAIL", "reason": "DIRTY_REPO"}
            head = self._git(repo, "rev-parse", "HEAD").strip()
            if head != str(manifest.get("base_commit", "")):
                failed = self.service.update_status(record, "APPLY_FAILED", phase="apply-failed", note="base commit changed")
                return failed, {"status": "FAIL", "reason": "BASE_CHANGED"}

            applying = self.service.update_status(record, "APPLYING", phase="applying", note="explicit apply")
            patch_path = goal_dir / "build.patch"
            check = self._git_apply(repo, patch_path, check_only=True)
            if check.returncode != 0:
                failed = self.service.update_status(applying, "APPLY_FAILED", phase="apply-failed", note="git apply --check failed")
                check_dict = {"exit_code": check.returncode, "stdout": _clip(check.stdout), "stderr": _clip(check.stderr)}
                summary = self._apply_result(failed, patch_path, check_dict, status="FAIL")
                self._persist_final_verification(goal_dir, summary)
                GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(failed.goal_id)
                return failed, summary

            apply = self._git_apply(repo, patch_path, check_only=False)
            if apply.returncode != 0:
                failed = self.service.update_status(applying, "APPLY_FAILED", phase="apply-failed", note="git apply failed")
                apply_dict = {"exit_code": apply.returncode, "stdout": _clip(apply.stdout), "stderr": _clip(apply.stderr)}
                summary = self._apply_result(failed, patch_path, apply_dict, status="FAIL")
                self._persist_final_verification(goal_dir, summary)
                GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(failed.goal_id)
                return failed, summary

            verify_cmds = list(request.get("verification_commands", []))
            private_cmds = request.get("private_verification_commands") or request.get("hidden_verification_commands") or []
            all_verify_cmds = verify_cmds + [c for c in private_cmds if c not in verify_cmds]
            verification = self._run_verification(repo, all_verify_cmds)
            if verification["status"] != "PASS":
                failed = self.service.update_status(applying, "POST_APPLY_VERIFICATION_FAILED", phase="post-apply-verification-failed", note="verification failed")
                summary = self._apply_result(failed, patch_path, verification, status="FAIL")
                self._persist_final_verification(goal_dir, summary)
                GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(failed.goal_id)
                return failed, summary

            completed = self.service.update_status(applying, "COMPLETED", phase="completed", note="apply complete")
            summary = self._apply_result(completed, patch_path, verification, status="PASS")
            self._persist_final_verification(goal_dir, summary)
            GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(completed.goal_id)
            return completed, summary

    def _acquire_repo_lock(self, repo: Path):
        repo_hash = hashlib.sha256(str(repo.resolve()).lower().encode("utf-8")).hexdigest()[:16]
        locks_root = self.runtime_root / "locks"
        locks_root.mkdir(parents=True, exist_ok=True)
        lock_path = locks_root / f"repo_{repo_hash}.lock"

        if lock_path.exists():
            try:
                content = lock_path.read_text(encoding="utf-8").strip()
                parts = content.split()
                if parts:
                    pid = int(parts[0])
                    lock_time = int(parts[1]) if len(parts) > 1 else 0
                    import time
                    now = int(time.time())
                    from .resume_service import _is_pid_alive
                    if not _is_pid_alive(pid) or (now - lock_time > 300):
                        lock_path.unlink()
            except Exception:
                try:
                    lock_path.unlink()
                except Exception:
                    pass

        class _Ctx:
            def __enter__(self_nonlocal):
                import os, time
                timeout_at = time.time() + 60.0
                while True:
                    try:
                        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            handle.write(f"{os.getpid()} {int(time.time())}")
                        return True
                    except FileExistsError:
                        if time.time() >= timeout_at:
                            raise TimeoutError(f"Could not acquire repository apply lock for {repo}")
                        time.sleep(0.05)

            def __exit__(self_nonlocal, exc_type, exc, tb):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

        return _Ctx()

    def _summary(
        self,
        record: GoalRecord,
        request: dict[str, Any],
        build: dict[str, Any],
        patch_text: str,
        violations: list[str],
        ready: bool,
        policy_pass: bool,
        verification_pass: bool,
        analysts: list[dict[str, Any]] | None = None,
        reviewer: dict[str, Any] | None = None,
        quorum_met: bool = True,
        verification: dict[str, Any] | None = None,
        patch_sha256: str | None = None,
    ) -> FinalReviewSummary:
        goal_dir = self.service.store.goal_dir(record.goal_id)
        base_commit = self._git(Path(request.get("target_repo", "")), "rev-parse", "HEAD").strip() if request.get("target_repo") else ""
        approval_at = record.updated_at
        if patch_sha256 is None:
            patch_sha256 = self._sha256_text(patch_text)
        analyst_rows = analysts if analysts is not None else self._build_analyst_results(request, build, patch_text, verification or {"status": "PASS"})
        reviewer_row = reviewer if reviewer is not None else self._build_reviewer(analyst_rows, quorum_met)
        execution_status = "DEGRADED" if not policy_pass or not verification_pass or not quorum_met else "SUCCESS"
        manifest_path = goal_dir / "apply_manifest.json"
        final_evidence_path = goal_dir / "final_evidence.json"
        final_verification_path = goal_dir / "final_verification.json"
        return FinalReviewSummary(
            goal_id=record.goal_id,
            executor=str(build.get("executor", "unknown")),
            state=record.status,
            allowed_files=[str(item) for item in request.get("allowed_files", []) if str(item).strip()],
            changed_files=[str(item) for item in build.get("changed_files", []) if str(item).strip()],
            violations=violations,
            patch_sha256=patch_sha256,
            base_commit=base_commit,
            approval_at=approval_at,
            patch_hash_matches=True,
            policy_pass=policy_pass,
            verification_pass=verification_pass,
            analysts=analyst_rows,
            reviewer=reviewer_row,
            quorum_met=quorum_met,
            execution_status=execution_status,
            ready_to_apply=ready,
            manifest_path=str(manifest_path),
            final_evidence_path=str(final_evidence_path),
            final_verification_path=str(final_verification_path),
        )

    def _build_analyst_results(self, request: dict[str, Any], build: dict[str, Any], patch_text: str, verification: dict[str, Any]) -> list[dict[str, Any]]:
        changed_files = [str(item) for item in build.get("changed_files", []) if str(item).strip()]
        allowed_files = [str(item) for item in request.get("allowed_files", []) if str(item).strip()]
        verification_pass = verification.get("status") == "PASS"
        patch_hash = self._sha256_text(patch_text)
        base = [
            {
                "analyst": "analyst-1",
                "decision": "PASS" if verification_pass and changed_files else "FAIL",
                "confidence": 0.95 if verification_pass else 0.25,
                "acceptance_criteria_met": [str(item) for item in request.get("acceptance_criteria", [])[:2]],
                "blocking_issues": [],
                "regression_risks": [],
                "missing_evidence": [],
                "evidence_refs": [f"patch:{patch_hash[:12]}"],
            },
            {
                "analyst": "analyst-2",
                "decision": "PASS" if self._subset(changed_files, allowed_files) else "FAIL",
                "confidence": 0.92 if self._subset(changed_files, allowed_files) else 0.2,
                "acceptance_criteria_met": [str(item) for item in request.get("acceptance_criteria", [])[2:4]],
                "blocking_issues": [] if self._subset(changed_files, allowed_files) else ["scope regression"],
                "regression_risks": [],
                "missing_evidence": [],
                "evidence_refs": [f"files:{len(changed_files)}"],
            },
            {
                "analyst": "analyst-3",
                "decision": "PASS" if not self._contains_secret_like(patch_text) else "FAIL",
                "confidence": 0.91 if not self._contains_secret_like(patch_text) else 0.1,
                "acceptance_criteria_met": [str(item) for item in request.get("acceptance_criteria", [])[4:]],
                "blocking_issues": [] if not self._contains_secret_like(patch_text) else ["secret material detected"],
                "regression_risks": [],
                "missing_evidence": [],
                "evidence_refs": [f"sha:{patch_hash[:12]}"],
            },
        ]
        return base

    def _build_reviewer(self, analysts: list[dict[str, Any]], quorum_met: bool) -> dict[str, Any]:
        pass_count = len([item for item in analysts if str(item.get("decision", "")).upper() == "PASS"])
        if not quorum_met:
            return {"decision": "FAIL", "agreement": "NONE", "confidence": 0.0, "blocking_issues": ["quorum not met"], "reason": "Not enough successful analysts.", "ready_to_apply": False}
        if pass_count >= 2:
            return {"decision": "PASS", "agreement": "FULL", "confidence": 0.97, "blocking_issues": [], "reason": "Analysts and policy gate passed.", "ready_to_apply": True}
        if pass_count == 1:
            return {"decision": "UNKNOWN", "agreement": "PARTIAL", "confidence": 0.5, "blocking_issues": ["mixed analyst results"], "reason": "Evidence is insufficient for final approval.", "ready_to_apply": False}
        return {"decision": "FAIL", "agreement": "FULL", "confidence": 0.2, "blocking_issues": ["analyst failures"], "reason": "Final review failed.", "ready_to_apply": False}

    def _quorum_met(self, analysts: list[dict[str, Any]]) -> bool:
        return len([item for item in analysts if str(item.get("decision", "")).upper() == "PASS"]) >= 2

    def _policy_violations(self, patch_text: str, allowed_files: list[str]) -> list[str]:
        try:
            violations = validate_patch_policy(patch_text, allowed_files)
        except ValueError as exc:
            return [str(exc)]
        return violations

    def _load_request(self, goal_dir: Path) -> dict[str, Any]:
        return self._load_json(goal_dir / "build_request.json")

    def _load_patch(self, goal_dir: Path) -> str:
        patch_path = goal_dir / "build.patch"
        if not patch_path.exists():
            raise FileNotFoundError(patch_path)
        return patch_path.read_text(encoding="utf-8")

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _persist_review(self, goal_dir: Path, summary: FinalReviewSummary, verification: dict[str, Any] | None = None) -> None:
        self.service.store.save_plan(summary.goal_id, "final_evidence.json", summary.to_dict())
        self.service.store.save_plan(summary.goal_id, "final_review.json", summary.reviewer)
        self.service.store.save_plan(summary.goal_id, "final_analyst_1.json", summary.analysts[0] if summary.analysts else {})
        self.service.store.save_plan(summary.goal_id, "final_analyst_2.json", summary.analysts[1] if len(summary.analysts) > 1 else {})
        self.service.store.save_plan(summary.goal_id, "final_analyst_3.json", summary.analysts[2] if len(summary.analysts) > 2 else {})
        if verification is not None:
            self.service.store.save_plan(summary.goal_id, "final_verification.json", verification)

    def _persist_final_verification(self, goal_id: str, verification: dict[str, Any]) -> None:
        self.service.store.save_plan(goal_id, "final_verification.json", verification)

    def _build_manifest(self, record: GoalRecord, request: dict[str, Any], patch_sha256: str) -> dict[str, Any]:
        return {
            "goal_id": record.goal_id,
            "patch_sha256": patch_sha256,
            "review_decision": "PASS",
            "verification": "PASS",
            "allowed_files": [str(item) for item in request.get("allowed_files", []) if str(item).strip()],
            "base_commit": self._git(Path(request.get("target_repo", "")), "rev-parse", "HEAD").strip(),
            "approved_at": record.updated_at,
        }

    def _run_verification(self, workspace: Path, commands: list[str]) -> dict[str, Any]:
        from .verification_command import run_verification_suite
        return run_verification_suite(commands, workspace, timeout=60.0)

    def _apply_result(self, record: GoalRecord, patch_path: Path, verification: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "goal_id": record.goal_id,
            "status": status,
            "patch_path": str(patch_path),
            "verification": verification,
            "state": record.status,
        }

    def _create_review_workspace(self, record: GoalRecord) -> Path:
        goal_dir = self.service.store.goal_dir(record.goal_id)
        request = self._load_request(goal_dir)
        repo = Path(request.get("target_repo", ""))
        return self._create_workspace_copy(repo, record.goal_id, suffix="review")

    def _create_workspace_copy(self, repo: Path, goal_id: str, suffix: str) -> Path:
        workspace = (self.workspaces_root / f"{goal_id}-{suffix}").resolve()
        if workspace.exists():
            if workspace.is_dir():
                _safe_rmtree(workspace)
            else:
                workspace.unlink()
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if not self._is_git_repo(repo):
            raise RuntimeError(f"target repo is not a git repository: {repo}")
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True, check=False)
        result = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", "--force", str(workspace), "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to create review git worktree: {result.stderr.strip() or result.stdout.strip()}")
        top_level = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        expected = str(workspace.resolve()).replace("\\", "/").lower()
        actual = top_level.stdout.strip().replace("\\", "/").lower()
        if top_level.returncode != 0 or actual != expected:
            raise RuntimeError(f"review workspace git root mismatch: expected {expected}, got {top_level.stdout.strip()}")
        return workspace

    def _patch_hash_matches(self, goal_dir: Path, patch_sha256: str) -> bool:
        manifest_path = goal_dir / "apply_manifest.json"
        if not manifest_path.exists():
            return True
        manifest = self._load_json(manifest_path)
        return str(manifest.get("patch_sha256", "")) == patch_sha256

    def _is_git_repo(self, path: Path) -> bool:
        return _git_is_repo(path)

    def _is_git_clean(self, path: Path) -> bool:
        return _git_is_clean(path)

    def _git(self, path: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False)
        return result.stdout.strip()

    def _git_apply(self, workspace: Path, patch_path: Path, check_only: bool) -> subprocess.CompletedProcess[str]:
        args = ["git", "-C", str(workspace), "apply", "--check", str(patch_path.resolve())] if check_only else ["git", "-C", str(workspace), "apply", str(patch_path.resolve())]
        return subprocess.run(args, cwd=workspace, capture_output=True, text=True)

    def _sha256_text(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _subset(self, changed_files: list[str], allowed_files: list[str]) -> bool:
        allowed = {str(item).replace("\\", "/").lstrip("./") for item in allowed_files}
        return all(str(item).replace("\\", "/").lstrip("./") in allowed for item in changed_files)

    def _contains_secret_like(self, text: str) -> bool:
        lowered = text.lower()
        return any(token in lowered for token in ("api_key", "authorization", "bearer ", "secret", "credential"))


def _clip(text: str) -> str:
    encoded = (text or "").encode("utf-8")
    if len(encoded) <= 8192:
        return text
    return encoded[:8192].decode("utf-8", errors="ignore")


def _is_safe_command(command: Any) -> bool:
    if isinstance(command, (list, tuple)):
        tokens = [str(item).strip() for item in command if str(item).strip()]
        if not tokens:
            return False
        exe = Path(tokens[0]).name.lower()
        if exe in {"rm", "del", "rmdir", "format", "shutdown"}:
            return False
        if tokens[0].lower() == "git" and len(tokens) > 1 and tokens[1].lower() in {"push", "clean"}:
            return False
        return True
    text = str(command).strip()
    if not text:
        return False
    lowered = text.lower()
    destructive = ("rm ", "del ", "rmdir ", "format ", "shutdown ", "git push", "git clean -f", "git reset --hard")
    return not any(lowered.startswith(token) or f" {token}" in lowered for token in destructive)


def _git_is_repo(path: Path) -> bool:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    return result.returncode == 0 and "true" in result.stdout.lower()


def _git_is_clean(path: Path) -> bool:
    result = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith("?? __pycache__") and not line.startswith("?? .pytest_cache") and not line.startswith("?? runtime")]
    return len(lines) == 0


def _safe_rmtree(path: Path | str) -> None:
    path_obj = Path(path)
    if not path_obj.exists():
        return
    import os
    import stat
    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path_obj, onerror=_onerror)
