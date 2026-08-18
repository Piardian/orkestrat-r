from __future__ import annotations

import json
import shutil
import subprocess
import shlex
from pathlib import Path
from typing import Any

from evidence.sanitizer import redact_text
from schemas import Review

from .codex import (
    CodexBuildArtifact,
    CodexRequest,
    MAX_MANUAL_ATTEMPTS,
    MAX_STD_BYTES,
    extract_unified_diff,
    build_codex_prompt,
    validate_patch_policy,
)
from .complexity import ComplexityAssessment
from .metrics_service import GoalMetricsService
from .model import GoalRecord
from .plan import GoalPlan
from .review import GoalReview
from .service import GoalService
from .verification_command import normalize_verification_command


class GoalCodexService:
    def __init__(self, service: GoalService | None = None, runtime_root: str | Path = "runtime") -> None:
        self.service = service or GoalService()
        self.runtime_root = Path(runtime_root)
        self.workspaces_root = self.runtime_root / "workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

    def prepare(self, goal_id: str) -> tuple[GoalRecord, CodexRequest, str]:
        record = self.service.read_goal(goal_id)
        if record.status.strip().upper() != "CODEX_REQUIRED":
            raise ValueError("Goal must be CODEX_REQUIRED before Codex preparation.")
        self._ensure_git_ready(record)
        goal_dir = self.service.store.goal_dir(goal_id)
        if self._next_attempt_index(goal_dir) > MAX_MANUAL_ATTEMPTS:
            raise ValueError("MANUAL_INTERVENTION_REQUIRED")
        plan = self._load_plan(goal_dir)
        review = self._load_review(goal_dir)
        complexity = self._load_complexity(goal_dir)
        evidence = self._load_json(goal_dir / "evidence.json")

        attempt_index = self._next_attempt_index(goal_dir)
        request, prompt = build_codex_prompt(record, plan, review, complexity, evidence, goal_dir, attempt_index)
        self.service.store.save_plan(goal_id, "codex_request.json", request.to_dict())
        self.service.store.save_text(goal_id, "codex_prompt.md", prompt)
        updated = self.service.update_status(record, "WAITING_CODEX", phase="codex-waiting", note=f"Codex prompt ready attempt={attempt_index}")
        return updated, request, prompt

    def submit(self, goal_id: str, response_text: str) -> tuple[GoalRecord, CodexBuildArtifact]:
        record = self.service.read_goal(goal_id)
        if record.status.strip().upper() != "WAITING_CODEX":
            raise ValueError("Goal must be WAITING_CODEX before Codex submit.")

        goal_dir = self.service.store.goal_dir(goal_id)
        request = self._load_codex_request(goal_dir)
        response_text = self._normalize_response(response_text)
        attempt_index = self._next_attempt_index(goal_dir)
        if attempt_index > MAX_MANUAL_ATTEMPTS:
            raise ValueError("MANUAL_INTERVENTION_REQUIRED")
        response_path = self.service.store.save_text(goal_id, f"codex_response_{attempt_index:03d}.txt", redact_text(response_text))

        received = self.service.update_status(record, "CODEX_RESPONSE_RECEIVED", phase="codex-response-received", note=f"attempt={attempt_index}")
        try:
            patch_text = extract_unified_diff(response_text)
        except ValueError:
            invalid = self.service.update_status(received, "CODEX_RESPONSE_INVALID", phase="codex-response-invalid", note="no unified diff found")
            artifact = self._artifact_from_state(invalid, response_path, status="CODEX_RESPONSE_INVALID", failure_type="NO_PATCH_FOUND")
            self._persist_artifact(invalid.goal_id, artifact, prompt=None)
            return invalid, artifact

        violations = validate_patch_policy(patch_text, request.allowed_files)
        if violations:
            blocked = self.service.update_status(received, "CODEX_POLICY_VIOLATION", phase="codex-policy-violation", note=", ".join(violations))
            artifact = self._artifact_from_state(blocked, response_path, status="CODEX_POLICY_VIOLATION", failure_type="UNAUTHORIZED_FILE")
            self._persist_artifact(blocked.goal_id, artifact, patch_text=patch_text)
            return blocked, artifact

        staging = self.service.update_status(received, "CODEX_PATCH_STAGING", phase="codex-patch-staging", note="patch accepted for staging")
        workspace = self._create_staging_workspace(staging)
        patch_path = goal_dir / "build.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        result = self._apply_and_verify(staging, request, workspace, patch_path, patch_text, response_path)
        final_status = result.status
        final_record = self.service.update_status(staging, final_status, phase="codex-final", note=result.failure_type or "verification complete")
        self._persist_artifact(final_record.goal_id, result, patch_text=patch_text)
        GoalMetricsService(self.service, runtime_root=self.runtime_root).refresh_goal(final_record.goal_id)
        return final_record, result

    def _apply_and_verify(
        self,
        record: GoalRecord,
        request: CodexRequest,
        workspace: Path,
        patch_path: Path,
        patch_text: str,
        response_path: Path,
    ) -> CodexBuildArtifact:
        check = subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=workspace, capture_output=True, text=True)
        if check.returncode != 0:
            return CodexBuildArtifact(
                goal_id=record.goal_id,
                executor="codex-manual",
                source="manual-response",
                changed_files=[],
                verification="FAIL",
                status="CODEX_PATCH_FAILED",
                failure_type="GIT_APPLY_CHECK_FAILED",
                patch_path=str(patch_path),
                patch_size=len(patch_text.encode("utf-8")),
                verification_result={"exit_code": check.returncode, "stdout": _clip(check.stdout), "stderr": _clip(check.stderr)},
                original_repo_modified=False,
                codex_response_path=str(response_path),
            )

        apply = subprocess.run(["git", "apply", str(patch_path)], cwd=workspace, capture_output=True, text=True)
        if apply.returncode != 0:
            return CodexBuildArtifact(
                goal_id=record.goal_id,
                executor="codex-manual",
                source="manual-response",
                changed_files=[],
                verification="FAIL",
                status="CODEX_PATCH_FAILED",
                failure_type="GIT_APPLY_FAILED",
                patch_path=str(patch_path),
                patch_size=len(patch_text.encode("utf-8")),
                verification_result={"exit_code": apply.returncode, "stdout": _clip(apply.stdout), "stderr": _clip(apply.stderr)},
                original_repo_modified=False,
                codex_response_path=str(response_path),
            )

        changed_files = self._changed_files(workspace)
        verification = self._run_verification(workspace, request.verification_commands)
        if verification["status"] != "PASS":
            return CodexBuildArtifact(
                goal_id=record.goal_id,
                executor="codex-manual",
                source="manual-response",
                changed_files=changed_files,
                verification="FAIL",
                status="CODEX_VERIFICATION_FAILED",
                failure_type="VERIFICATION_FAILED",
                patch_path=str(patch_path),
                patch_size=len(patch_text.encode("utf-8")),
                verification_result=verification,
                original_repo_modified=False,
                codex_response_path=str(response_path),
            )

        return CodexBuildArtifact(
            goal_id=record.goal_id,
            executor="codex-manual",
            source="manual-response",
            changed_files=changed_files,
            verification="PASS",
            status="BUILT_PENDING_REVIEW",
            failure_type=None,
            patch_path=str(patch_path),
            patch_size=len(patch_text.encode("utf-8")),
            verification_result=verification,
            original_repo_modified=False,
            codex_response_path=str(response_path),
        )

    def _run_verification(self, workspace: Path, commands: list[str]) -> dict[str, Any]:
        from .verification_command import run_verification_suite
        return run_verification_suite(commands, workspace, timeout=60.0)

    def _create_staging_workspace(self, record: GoalRecord) -> Path:
        if not self._is_git_repo(record.repo) or not self._is_git_clean(record.repo):
            raise RuntimeError("CODEX_WORKSPACE_BLOCKED")
        workspace = self.workspaces_root / record.goal_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "-C", record.repo, "worktree", "add", "--detach", str(workspace), "HEAD"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "CODEX_WORKSPACE_BLOCKED")
        return workspace

    def _persist_artifact(self, goal_id: str, artifact: CodexBuildArtifact, patch_text: str | None = None, prompt: str | None = None) -> None:
        self.service.store.save_plan(goal_id, "build.json", artifact.to_dict())
        if patch_text is not None:
            self.service.store.save_text(goal_id, "build.patch", patch_text)
        if artifact.verification_result is not None:
            self.service.store.save_plan(goal_id, "verification.json", artifact.verification_result)

    def _artifact_from_state(self, record: GoalRecord, response_path: Path, status: str, failure_type: str) -> CodexBuildArtifact:
        return CodexBuildArtifact(
            goal_id=record.goal_id,
            executor="codex-manual",
            source="manual-response",
            changed_files=[],
            verification="FAIL",
            status=status,
            failure_type=failure_type,
            patch_path=None,
            patch_size=0,
            verification_result=None,
            original_repo_modified=False,
            codex_response_path=str(response_path),
        )

    def _load_plan(self, goal_dir: Path) -> GoalPlan:
        return GoalPlan.from_dict(self._load_json(goal_dir / "plan.json"))

    def _load_review(self, goal_dir: Path) -> Review:
        return Review.from_dict(self._load_json(goal_dir / "review.json"))

    def _load_complexity(self, goal_dir: Path) -> ComplexityAssessment:
        return ComplexityAssessment.from_dict(self._load_json(goal_dir / "complexity.json"))

    def _load_codex_request(self, goal_dir: Path) -> CodexRequest:
        return CodexRequest(**self._load_json(goal_dir / "codex_request.json"))

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _normalize_response(self, response_text: str) -> str:
        text = response_text.replace("\r\n", "\n")
        if "::END_CODEX::" in text:
            text = text.split("::END_CODEX::", 1)[0]
        return text.strip()

    def _next_attempt_index(self, goal_dir: Path) -> int:
        count = len(list(goal_dir.glob("codex_response_*.txt")))
        return count + 1

    def _changed_files(self, workspace: Path) -> list[str]:
        diff = subprocess.run(["git", "diff", "--name-only"], cwd=workspace, capture_output=True, text=True)
        files = [line.strip().replace("\\", "/") for line in diff.stdout.splitlines() if line.strip()]
        return files

    def _ensure_git_ready(self, record: GoalRecord) -> None:
        if not self._is_git_repo(record.repo) or not self._is_git_clean(record.repo):
            raise RuntimeError("CODEX_WORKSPACE_BLOCKED")

    def _is_git_repo(self, path: str | Path) -> bool:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
        return result.returncode == 0 and "true" in result.stdout.lower()

    def _is_git_clean(self, path: str | Path) -> bool:
        result = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
        if result.returncode != 0:
            return False
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        lines = [line for line in lines if not line.startswith("?? __pycache__") and not line.startswith("?? .pytest_cache") and not line.startswith("?? runtime")]
        return len(lines) == 0


def _clip(text: str) -> str:
    encoded = (text or "").encode("utf-8")
    if len(encoded) <= MAX_STD_BYTES:
        return text
    return encoded[:MAX_STD_BYTES].decode("utf-8", errors="ignore")


def _is_safe_command(command: str) -> bool:
    banned = ("&&", "||", "|", ">", "<", ";", "`", "$(", "rm ", "del ", "rmdir ", "format ", "shutdown ", "git push")
    lowered = command.lower()
    return not any(token in lowered for token in banned)
