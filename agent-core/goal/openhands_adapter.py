from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .builder import BuilderAdapter, BuilderRequest, BuilderResult
from .builder_policy import BuilderPolicy
from .builder_rate_limiter import BuilderRateLimitConfig, BuilderRateLimiter

LIVE_VALIDATION_ENV = "AGENT_CORE_OPENHANDS_LIVE_VALIDATION"


class OpenHandsUnavailableError(RuntimeError):
    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class OpenHandsBuilderAdapter(BuilderAdapter):
    def __init__(self, policy: BuilderPolicy | None = None) -> None:
        self.policy = policy or BuilderPolicy()
        self.rate_limiter = BuilderRateLimiter(
            BuilderRateLimitConfig(
                enabled=self.policy.rate_limit_enabled,
                requests_per_minute=self.policy.rate_limit_requests_per_minute,
                safety_margin=self.policy.rate_limit_safety_margin,
                window_seconds=self.policy.rate_limit_window_seconds,
                retry_attempts=self.policy.rate_limit_retry_attempts,
                retry_backoff_seconds=tuple(self.policy.rate_limit_backoff_seconds or [5.0, 15.0, 30.0]),
            )
        )

    def execute(self, request: BuilderRequest) -> BuilderResult:
        sdk_version = _package_version("openhands-sdk")
        tools_version = _package_version("openhands-tools")
        if sdk_version is None or tools_version is None:
            raise OpenHandsUnavailableError("OpenHands SDK packages are not installed.", kind="OPENHANDS_NOT_INSTALLED")
        if sdk_version != tools_version:
            raise OpenHandsUnavailableError(
                f"OpenHands SDK version mismatch: sdk={sdk_version}, tools={tools_version}",
                kind="OPENHANDS_VERSION_MISMATCH",
            )
        return self._execute_live(request, sdk_version=sdk_version, tools_version=tools_version)

    def _build_task_prompt(self, request: BuilderRequest) -> str:
        goal_text = request.goal.get("goal") if isinstance(request.goal, dict) else str(request.goal)
        allowed = "\n".join(f"- {f}" for f in request.allowed_files) if request.allowed_files else "- NONE"
        criteria = "\n".join(f"- {c}" for c in request.acceptance_criteria) if request.acceptance_criteria else "- Satisfy the goal objective."
        constraints = "\n".join(f"- {c}" for c in request.constraints) if request.constraints else "- Do not modify any unauthorized files."
        verification = "\n".join(f"- {v}" for v in request.verification_commands) if request.verification_commands else "- Ensure existing tests pass."

        prompt = f"""You are working in a staging git workspace.

Task:
{goal_text}

Allowed Files to Modify:
{allowed}

Rules:
- Do not modify any file outside the allowed files list.
- Do not create unauthorized new files.
- Use only local file editing tools.
- Keep changes minimal and focused directly on the task.
- Stop when the edits are complete.

Acceptance Criteria:
{criteria}

Constraints:
{constraints}

Verification Expectation:
{verification}
"""
        return prompt

    def _execute_live(self, request: BuilderRequest, *, sdk_version: str, tools_version: str) -> BuilderResult:
        try:
            from openhands.sdk.agent import Agent
            from openhands.sdk.conversation import Conversation
            from openhands.sdk.llm import LLM
            from openhands.sdk.tool.spec import Tool
            from openhands.sdk.workspace.local import LocalWorkspace
            from openhands.tools.file_editor import FileEditorTool
            from openhands.tools.task_tracker import TaskTrackerTool
            from openhands.tools.preset.default import get_default_tools
        except Exception as exc:  # pragma: no cover - import failure is runtime-specific
            raise OpenHandsUnavailableError(f"OpenHands live imports failed: {exc}", kind="OPENHANDS_IMPORT_FAILED") from exc

        profile = self._load_profile()
        secret_env = profile.get("secret_env")
        api_key = os.getenv(secret_env) if secret_env else None
        if secret_env and not api_key:
            try:
                from dotenv import load_dotenv
                dotenv_path = Path(__file__).resolve().parents[1] / ".env"
                if dotenv_path.exists():
                    load_dotenv(dotenv_path=dotenv_path, override=False)
                    api_key = os.getenv(secret_env)
            except Exception:
                pass
        if secret_env and not api_key:
            raise OpenHandsUnavailableError(
                f"Missing configured secret environment variable: {secret_env}",
                kind="OPENHANDS_SECRET_MISSING",
            )

        workspace_path = Path(request.workspace_path)
        workspace_path.mkdir(parents=True, exist_ok=True)
        target_repo_path = Path(request.target_repo)
        original_repo_before = self._repo_snapshot(target_repo_path)
        workspace_before = self._workspace_snapshot(workspace_path)
        llm = self._build_llm(profile, api_key)
        tools = [tool for tool in get_default_tools(enable_browser=False) if tool.name in {FileEditorTool.name, TaskTrackerTool.name}]
        agent = Agent(llm=llm, tools=tools)
        conversation = Conversation(
            agent,
            workspace=LocalWorkspace(working_dir=workspace_path),
            max_iteration_per_run=max(1, min(self.policy.max_runtime_seconds // 30, 12)),
            delete_on_close=False,
        )

        task_prompt = self._build_task_prompt(request)
        conversation.send_message(task_prompt)
        max_run_attempts = max(1, int(self.policy.rate_limit_retry_attempts or 3))
        backoffs = list(self.policy.rate_limit_backoff_seconds or [10.0, 30.0, 60.0])
        for run_attempt in range(max_run_attempts):
            try:
                conversation.run()
                break
            except Exception as exc:
                err_text = str(exc).lower()
                is_rate_limit = "429" in err_text or "rate limit" in err_text or "resource_exhausted" in err_text or "quota" in err_text
                if is_rate_limit and run_attempt + 1 < max_run_attempts:
                    delay = backoffs[min(run_attempt, len(backoffs) - 1)] if backoffs else 15.0
                    print(f"OpenHands conversation rate-limited. Waiting {delay:.1f}s before retry (attempt {run_attempt + 1}/{max_run_attempts})...", flush=True)
                    time.sleep(delay)
                    continue
                conversation.close()
                raise
        conversation.close()
        builder_runtime = self._extract_runtime_stats(llm, conversation)

        workspace_after = self._workspace_snapshot(workspace_path)
        changed_files = self._workspace_changed_files(workspace_before, workspace_after)
        unauthorized = [item for item in changed_files if item not in request.allowed_files]
        verification = self._run_verification(workspace_path, request.verification_commands)
        patch_path = workspace_path / "build.patch"
        patch_text = self._git_patch(workspace_path, request.allowed_files)
        patch_path.write_text(patch_text, encoding="utf-8")
        original_repo_after = self._repo_snapshot(target_repo_path)
        original_repo_modified = original_repo_before != original_repo_after

        status = "BUILT_PENDING_REVIEW"
        failure_type = None
        if unauthorized and not request.allow_new_files:
            status = "BUILDER_POLICY_VIOLATION"
            failure_type = f"unauthorized files: {', '.join(unauthorized)}"
        elif verification["status"] != "PASS":
            status = "BUILD_FAILED"
            failure_type = "verification failed"
        elif original_repo_modified:
            status = "BUILD_FAILED"
            failure_type = "original repo modified"
        elif not patch_text.strip():
            status = "BUILD_FAILED"
            failure_type = "empty patch"

        return BuilderResult(
            goal_id=request.goal_id,
            status=status,
            failure_type=failure_type,
            recommended_executor="openhands",
            changed_files=changed_files,
            unauthorized_files=unauthorized,
            patch_path=str(patch_path),
            patch_size=patch_path.stat().st_size if patch_path.exists() else 0,
            verification_status=verification["status"],
            verification_commands=list(request.verification_commands),
            verification_result=verification,
            openhands_executed=True,
            terminal_tool_enabled=False,
            original_repo_modified=original_repo_modified,
            provider_requests=int(builder_runtime.get("provider_requests") or 0),
            provider_retries=int(builder_runtime.get("provider_retries") or 0),
            builder_rate_limit_waits=int(builder_runtime.get("builder_rate_limit_waits") or 0),
            builder_rate_limit_wait_seconds=float(builder_runtime.get("builder_rate_limit_wait_seconds") or 0.0),
            provider_429_count=int(builder_runtime.get("provider_429_count") or 0),
            provider_503_count=int(builder_runtime.get("provider_503_count") or 0),
            provider_timeout_count=int(builder_runtime.get("provider_timeout_count") or 0),
            quota_exhausted_count=int(builder_runtime.get("quota_exhausted_count") or 0),
            retry_exhausted=bool(builder_runtime.get("retry_exhausted")),
        )

    def _extract_runtime_stats(self, llm: Any, conversation: Any) -> dict[str, Any]:
        stats = self._builder_runtime_state()
        if hasattr(llm, "runtime"):
            try:
                stats.update(llm.runtime())
            except Exception:
                pass
        if int(stats.get("provider_requests") or 0) <= 0:
            iterations = getattr(conversation, "iterations", None)
            if isinstance(iterations, list) and len(iterations) > 0:
                stats["provider_requests"] = len(iterations)
            else:
                events = getattr(conversation, "events", None)
                if isinstance(events, list) and len(events) > 0:
                    stats["provider_requests"] = len(events)
                else:
                    stats["provider_requests"] = 1
        return stats

    def _load_profile(self) -> dict[str, Any]:
        config_path = Path(__file__).resolve().parents[1] / "config" / "profiles.yaml"
        try:
            import yaml
        except Exception as exc:  # pragma: no cover - runtime dependency issue
            raise OpenHandsUnavailableError(f"OpenHands profile loader unavailable: {exc}", kind="OPENHANDS_PROFILE_LOAD_FAILED") from exc

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        profile_id = self.policy.profile
        for item in raw.get("profiles", []):
            if str(item.get("id", "")) == profile_id:
                return {
                    "id": str(item.get("id", profile_id)),
                    "provider": str(item.get("provider", "gemini")),
                    "model": str(item.get("model", "")),
                    "base_url": item.get("base_url") or None,
                    "secret_env": item.get("secret_env") or None,
                }
        raise OpenHandsUnavailableError(f"Builder profile not found: {profile_id}", kind="OPENHANDS_PROFILE_NOT_FOUND")

    def _build_llm(self, profile: dict[str, Any], api_key: str | None):
        from openhands.sdk.llm import LLM

        provider = profile.get("provider")
        model = profile.get("model")
        base_url = profile.get("base_url")
        if provider == "gemini" and model and not str(model).startswith("gemini/"):
            model = f"gemini/{model}"
        return LLM(
            model=str(model),
            api_key=api_key,
            base_url=base_url,
            num_retries=5,
            retry_min_wait=3,
            retry_max_wait=60,
            timeout=self.policy.max_runtime_seconds,
            max_message_chars=4000,
            max_input_tokens=16384,
            max_output_tokens=1024,
            temperature=0.0,
            top_p=1.0,
            stream=False,
            reasoning_effort="none",
            log_completions=False,
        )

    def _builder_runtime_state(self) -> dict[str, Any]:
        return {
            "provider_requests": 0,
            "provider_retries": 0,
            "builder_rate_limit_waits": 0,
            "builder_rate_limit_wait_seconds": 0.0,
            "provider_429_count": 0,
            "provider_503_count": 0,
            "provider_timeout_count": 0,
            "quota_exhausted_count": 0,
            "retry_exhausted": False,
        }

    def _workspace_changed_files(self, before: dict[str, str], after: dict[str, str]) -> list[str]:
        changed = sorted({path for path in set(before) | set(after) if before.get(path) != after.get(path)})
        return changed

    def _git_patch(self, workspace_path: Path, allowed_files: list[str]) -> str:
        if allowed_files:
            subprocess.run(["git", "-C", str(workspace_path), "add", "-N", "--", *allowed_files], capture_output=True, text=True, check=False)
            args = ["git", "-C", str(workspace_path), "diff", "--binary", "--", *allowed_files]
        else:
            subprocess.run(["git", "-C", str(workspace_path), "add", "-N", "."], capture_output=True, text=True, check=False)
            args = ["git", "-C", str(workspace_path), "diff", "--binary"]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout

    def _run_verification(self, workspace_path: Path, commands: list[str]) -> dict[str, Any]:
        from .verification_command import run_verification_suite
        return run_verification_suite(commands, workspace_path, timeout=float(self.policy.max_runtime_seconds))

    def _repo_snapshot(self, repo_path: Path) -> dict[str, Any]:
        try:
            head = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            status = subprocess.run(
                ["git", "-C", str(repo_path), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True,
                text=True,
                check=False,
            )
            staged = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
                check=False,
            )
            tracked = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--name-only"],
                capture_output=True,
                text=True,
                check=False,
            )
            return {
                "head": head.stdout.strip(),
                "status": status.stdout.splitlines(),
                "staged": [line.strip() for line in staged.stdout.splitlines() if line.strip()],
                "tracked": [line.strip() for line in tracked.stdout.splitlines() if line.strip()],
            }
        except FileNotFoundError:
            return {"head": "", "status": [], "staged": [], "tracked": []}

    def _workspace_snapshot(self, workspace_path: Path) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        if not workspace_path.exists():
            return snapshot
        for path in sorted(workspace_path.rglob("*")):
            if not path.is_file():
                continue
            if ".git" in path.parts or path.name == "build.patch":
                continue
            try:
                relative = str(path.relative_to(workspace_path)).replace("\\", "/")
                snapshot[relative] = _file_digest(path)
            except Exception:
                continue
        return snapshot


def _package_version(package_name: str) -> str | None:
    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _file_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class _RateLimitedLLM:
    def __init__(
        self,
        *,
        llm: Any,
        limiter: BuilderRateLimiter,
        scope_id: str,
        profile_id: str,
        provider: str,
        model: str,
        policy: BuilderPolicy,
    ) -> None:
        self._llm = llm
        self._limiter = limiter
        self._scope_id = scope_id
        self._profile_id = profile_id
        self._provider = provider
        self._model = model
        self._policy = policy
        self._runtime = {
            "provider_requests": 0,
            "provider_retries": 0,
            "builder_rate_limit_waits": 0,
            "builder_rate_limit_wait_seconds": 0.0,
            "provider_429_count": 0,
            "provider_503_count": 0,
            "provider_timeout_count": 0,
            "quota_exhausted_count": 0,
            "retry_exhausted": False,
        }

    def completion(self, *args: Any, **kwargs: Any) -> Any:
        backoffs = list(self._policy.rate_limit_backoff_seconds or [5.0, 15.0, 30.0])
        last_exc: Exception | None = None
        for attempt in range(max(1, int(self._policy.rate_limit_retry_attempts))):
            wait_seconds = self._limiter.reserve(self._scope_id)
            if wait_seconds > 0:
                self._runtime["builder_rate_limit_waits"] += 1
                self._runtime["builder_rate_limit_wait_seconds"] += wait_seconds
                print(
                    f"BUILDER RATE LIMIT | profile={self._profile_id} provider={self._provider} model={self._model} "
                    f"scope={self._scope_id} waiting={wait_seconds:.1f}s",
                    flush=True,
                )
            self._runtime["provider_requests"] += 1
            try:
                response = self._llm.completion(*args, **kwargs)
                self._merge_response_metrics(response)
                return response
            except Exception as exc:
                last_exc = exc
                classification = _classify_builder_provider_error(exc)
                if classification in {"AUTH_ERROR", "MODEL_NOT_FOUND", "INVALID_REQUEST"}:
                    self._runtime["retry_exhausted"] = True
                    raise
                if classification == "RATE_LIMIT":
                    self._runtime["provider_429_count"] += 1
                    if _looks_like_quota_exhausted(exc):
                        self._runtime["quota_exhausted_count"] += 1
                elif classification == "SERVICE_UNAVAILABLE":
                    self._runtime["provider_503_count"] += 1
                elif classification == "TIMEOUT":
                    self._runtime["provider_timeout_count"] += 1
                elif classification != "TRANSIENT":
                    self._runtime["retry_exhausted"] = attempt + 1 >= max(1, int(self._policy.rate_limit_retry_attempts))
                    raise
                if attempt + 1 >= max(1, int(self._policy.rate_limit_retry_attempts)):
                    self._runtime["retry_exhausted"] = True
                    raise
                self._runtime["provider_retries"] += 1
                delay = backoffs[min(attempt, len(backoffs) - 1)] if backoffs else 5.0
                print(
                    f"BUILDER PROVIDER RETRY | profile={self._profile_id} provider={self._provider} model={self._model} "
                    f"classification={classification} attempt={attempt + 1} sleep={delay:.1f}s",
                    flush=True,
                )
                time.sleep(delay)
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenHands LLM completion failed")

    def runtime(self) -> dict[str, Any]:
        return dict(self._runtime)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._llm, item)

    def _merge_response_metrics(self, response: Any) -> None:
        runtime = self.runtime()
        for key, value in runtime.items():
            if isinstance(value, bool):
                continue
            if hasattr(response, key):
                try:
                    current = int(getattr(response, key) or 0)
                except Exception:
                    current = 0
                try:
                    setattr(response, key, current + int(value or 0))
                except Exception:
                    pass
        for key, value in runtime.items():
            if hasattr(response, key):
                try:
                    setattr(response, key, value)
                except Exception:
                    pass


def _classify_builder_provider_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return "AUTH_ERROR"
    if "404" in text or "not found" in text or "model" in text and "unknown" in text:
        return "MODEL_NOT_FOUND"
    if "400" in text or "invalid" in text or "malformed" in text or "bad request" in text:
        return "INVALID_REQUEST"
    if "429" in text or "rate limit" in text or "quota" in text:
        return "RATE_LIMIT"
    if "503" in text or "service unavailable" in text:
        return "SERVICE_UNAVAILABLE"
    if "timeout" in text or "timed out" in text or "connection" in text or "temporarily unavailable" in text:
        return "TIMEOUT"
    if "retry" in text or "transient" in text:
        return "TRANSIENT"
    return "UNKNOWN"


def _looks_like_quota_exhausted(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "quota" in text or "free_tier" in text or "resource_exhausted" in text
