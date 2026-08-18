from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from dataclasses import replace

from .builder import BuilderAdapter, BuilderRequest, BuilderResult
from .builder_policy import BuilderPolicy, load_builder_policy
from .complexity import ComplexityAssessment
from .metrics_service import GoalMetricsService
from .model import GoalRecord
from .service import GoalService
from .openhands_adapter import OpenHandsBuilderAdapter, OpenHandsUnavailableError


class GoalBuilderService:
    def __init__(
        self,
        service: GoalService | None = None,
        router: Any | None = None,
        adapter: BuilderAdapter | None = None,
        policy: BuilderPolicy | None = None,
        execution_mode: str | None = None,
        runtime_root: str | Path = "runtime/workspaces",
    ) -> None:
        self.service = service or GoalService()
        self.router = router
        self.policy = policy or load_builder_policy()
        if execution_mode:
            self.policy = replace(self.policy, mode=execution_mode)
        self.adapter = adapter or OpenHandsBuilderAdapter(self.policy)
        self.runtime_root = Path(runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def dry_run(self, goal_id: str) -> tuple[GoalRecord, BuilderRequest]:
        record, request, _ = self._prepare(goal_id, persist=False)
        return record, request

    def execute(self, goal_id: str) -> tuple[GoalRecord, BuilderRequest, BuilderResult]:
        record, request, goal_dir = self._prepare(goal_id, persist=True)
        if record.status.strip().upper() != "READY_FOR_OPENHANDS":
            raise ValueError("Goal must be READY_FOR_OPENHANDS before builder execution.")
        if not self._is_git_repo(record.repo):
            blocked = self.service.update_status(record, "BUILDER_BLOCKED", phase="builder-blocked", note="target repo is not a git repository")
            self._persist(blocked, request)
            raise RuntimeError("BUILDER_BLOCKED")
        preflight_warnings = self._preflight_warnings(record.repo)
        if preflight_warnings and self.policy.mode != "relaxed-acceptance":
            blocked = self.service.update_status(record, "BUILDER_BLOCKED", phase="builder-blocked", note="target repo working tree is dirty")
            self._persist(blocked, request)
            raise RuntimeError("BUILDER_BLOCKED")

        building = self.service.update_status(record, "BUILDING", phase="building", note="builder started")
        workspace = self._create_workspace(building)
        request = self._with_workspace(request, workspace)
        self._persist(building, request)
        try:
            result = self.adapter.execute(request)
            if preflight_warnings:
                result = self._with_preflight_warnings(result, preflight_warnings)
            violation = self._policy_violation(request, result)
            if violation:
                failed = self.service.update_status(building, "BUILDER_POLICY_VIOLATION", phase="build-failed", note=violation)
                self._persist(failed, request, result=result)
                GoalMetricsService(self.service).refresh_goal(failed.goal_id)
                return failed, request, result
            if self._build_is_success(result, violation=None):
                built = self.service.update_status(building, "BUILT_PENDING_REVIEW", phase="built-pending-review", note="builder completed")
                self._persist(built, request, result=result)
                GoalMetricsService(self.service).refresh_goal(built.goal_id)
                return built, request, result
            if result.verification_status and result.verification_status.upper() == "FAIL":
                failed = self.service.update_status(building, "BUILD_FAILED", phase="build-failed", note="verification failed")
                self._persist(failed, request, result=result)
                GoalMetricsService(self.service).refresh_goal(failed.goal_id)
                return failed, request, result
            if result.failure_type:
                failed = self.service.update_status(building, result.status, phase="build-failed", note=result.failure_type)
                self._persist(failed, request, result=result)
                GoalMetricsService(self.service).refresh_goal(failed.goal_id)
                return failed, request, result
            failed = self.service.update_status(building, "BUILD_FAILED", phase="build-failed", note="builder result incomplete")
            self._persist(failed, request, result=result)
            GoalMetricsService(self.service).refresh_goal(failed.goal_id)
            return failed, request, result
        except OpenHandsUnavailableError as exc:
            failed = self.service.update_status(building, "BUILD_FAILED", phase="build-failed", note=exc.kind)
            self._persist(failed, request)
            GoalMetricsService(self.service).refresh_goal(failed.goal_id)
            raise
        except Exception as exc:
            failed = self.service.update_status(building, "BUILD_FAILED", phase="build-failed", note=str(exc))
            self._persist(failed, request)
            GoalMetricsService(self.service).refresh_goal(failed.goal_id)
            raise

    def _prepare(self, goal_id: str, persist: bool = False) -> tuple[GoalRecord, BuilderRequest, Path]:
        record = self.service.read_goal(goal_id)
        if record.status.strip().upper() != "READY_FOR_OPENHANDS":
            raise ValueError("Goal must be READY_FOR_OPENHANDS before builder execution.")
        goal_dir = self.service.store.goal_dir(goal_id)
        goal = self._load(goal_dir / "goal.json")
        plan = self._load(goal_dir / "plan.json")
        review = self._load(goal_dir / "review.json")
        complexity = self._load(goal_dir / "complexity.json")
        evidence = self._load(goal_dir / "evidence.json")

        allowed_files = self._allowed_files(plan)
        forbidden_patterns = list(self.policy.forbidden_patterns)
        forbidden_areas = self._forbidden_areas(plan, review, complexity)
        request = BuilderRequest(
            goal_id=goal_id,
            goal=goal,
            plan=plan,
            review=review,
            complexity=complexity,
            evidence=evidence,
            mode=self.policy.mode,
            builder_profile=self.policy.profile,
            allowed_files=allowed_files,
            forbidden_patterns=forbidden_patterns,
            forbidden_areas=forbidden_areas,
            acceptance_criteria=plan.get("acceptance_criteria", []),
            verification_commands=plan.get("verification", []),
            constraints=plan.get("constraints", []),
            workspace_path=str((self.runtime_root / goal_id).resolve()),
            target_repo=record.repo,
            allow_new_files=False,
        )
        if persist:
            self._persist(record, request)
        return record, request, goal_dir

    def _with_workspace(self, request: BuilderRequest, workspace: Path) -> BuilderRequest:
        payload = request.to_dict()
        payload["workspace_path"] = str(workspace.resolve())
        return BuilderRequest(**payload)

    def _persist(self, record: GoalRecord, request: BuilderRequest, result: BuilderResult | None = None) -> None:
        goal_dir = self.service.store.goal_dir(record.goal_id)
        goal_dir.mkdir(parents=True, exist_ok=True)
        self.service.store.save_plan(record.goal_id, "build_request.json", request.to_dict())
        if result is not None:
            self.service.store.save_plan(record.goal_id, "build.json", result.to_dict())
            if result.verification_result is not None:
                self.service.store.save_plan(record.goal_id, "verification.json", result.verification_result)
            if result.patch_path:
                patch_path = Path(result.patch_path)
                if patch_path.exists():
                    shutil.copyfile(patch_path, goal_dir / "build.patch")

    def _create_workspace(self, record: GoalRecord) -> Path:
        workspace = (self.runtime_root / record.goal_id).resolve()
        source = Path(record.repo)
        if workspace.exists():
            if workspace.is_dir():
                _safe_rmtree(workspace)
            else:
                workspace.unlink()
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            raise RuntimeError("target repo does not exist")
        if not self._is_git_repo(source):
            raise RuntimeError("target repo is not a git repository")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "--detach",
                "--force",
                str(workspace),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to create git worktree: {result.stderr.strip() or result.stdout.strip()}")
        top_level = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        expected = str(workspace.resolve())
        actual = top_level.stdout.strip().replace("\\", "/")
        if top_level.returncode != 0 or actual.lower() != expected.replace("\\", "/").lower():
            raise RuntimeError(f"workspace git root mismatch: expected {expected}, got {top_level.stdout.strip() or 'UNKNOWN'}")
        return workspace

    def _preflight_warnings(self, repo_path: str | Path) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        repo = Path(repo_path)
        if self._is_git_repo(repo):
            dirty = self._git_status_porcelain(repo)
            if dirty:
                warnings.append({"code": "DIRTY_TARGET_REPO", "bypassed": self.policy.mode == "relaxed-acceptance", "mode": self.policy.mode})
                if self._has_untracked_entries(dirty):
                    warnings.append({"code": "UNTRACKED_FILES", "bypassed": self.policy.mode == "relaxed-acceptance", "mode": self.policy.mode})
        return warnings

    def _allowed_files(self, plan: dict[str, Any]) -> list[str]:
        allowed = plan.get("allowed_files", [])
        if not allowed:
            # Fallback for old plans where allowed_files was not distinct
            allowed = plan.get("candidate_files", [])
        if plan.get("patch_expected", True):
            return allowed
        return allowed

    def _forbidden_areas(self, plan: dict[str, Any], review: dict[str, Any], complexity: dict[str, Any]) -> list[str]:
        return [
            ".env",
            "credentials",
            "secrets",
            "trading logic",
            "unrelated modules",
            f"severity:{complexity.get('severity', 'UNKNOWN')}",
            f"review:{review.get('final_verdict', 'UNKNOWN')}",
        ]

    def _policy_violation(self, request: BuilderRequest, result: BuilderResult) -> str | None:
        unauthorized = [item for item in result.changed_files if item not in request.allowed_files]
        if unauthorized and not request.allow_new_files:
            return f"unauthorized files: {', '.join(unauthorized)}"
        if result.patch_size and result.patch_size > self.policy.max_patch_bytes:
            return "patch too large"
        return None

    def _build_is_success(self, result: BuilderResult, violation: str | None) -> bool:
        if violation:
            return False
        if result.failure_type:
            return False
        if not result.openhands_executed:
            return False
        if not result.verification_status or result.verification_status.upper() != "PASS":
            return False
        if result.original_repo_modified:
            return False
        if not result.patch_path:
            return False
        patch = Path(result.patch_path)
        if not patch.exists() or patch.stat().st_size <= 0:
            return False
        if not result.changed_files:
            return False
        return True

    def _load(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _is_git_repo(self, path: str | Path) -> bool:
        repo = Path(path)
        try:
            result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, check=False)
            return result.returncode == 0 and "true" in result.stdout.lower()
        except FileNotFoundError:
            return False

    def _is_git_clean(self, path: str | Path) -> bool:
        return not self._preflight_warnings(path)

    def _git_status_porcelain(self, path: str | Path) -> list[str]:
        repo = Path(path)
        try:
            result = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=False)
            if result.returncode != 0:
                return []
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return [line for line in lines if not line.startswith("?? __pycache__") and not line.startswith("?? .pytest_cache") and not line.startswith("?? runtime")]
        except FileNotFoundError:
            return []

    def _has_untracked_entries(self, porcelain: list[str]) -> bool:
        return any(line.startswith("??") for line in porcelain)

    def _with_preflight_warnings(self, result: BuilderResult, warnings: list[dict[str, Any]]) -> BuilderResult:
        payload = result.to_dict()
        payload["preflight_warnings"] = warnings
        return BuilderResult(**payload)


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
