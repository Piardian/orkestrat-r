from __future__ import annotations

import os
import platform
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Any
import uuid

from .builder import BuilderRequest, BuilderResult
from .openhands_adapter import OpenHandsBuilderAdapter, OpenHandsUnavailableError


class DockerOpenHandsBuilderAdapter(OpenHandsBuilderAdapter):
    """OpenHands builder that never gives the agent direct host execution.

    The editing agent runs in OpenHands ``DockerWorkspace`` with only file-editing
    tools. The resulting patch is then verified in a second disposable Docker
    container with no network, no host secrets and bounded resources.
    """

    def _execute_live(self, request: BuilderRequest, *, sdk_version: str, tools_version: str) -> BuilderResult:
        if shutil.which("docker") is None:
            raise OpenHandsUnavailableError("Docker is required for sandboxed OpenHands execution.", kind="DOCKER_NOT_INSTALLED")

        try:
            from openhands.sdk.agent import Agent
            from openhands.sdk.conversation import Conversation
            from openhands.tools.file_editor import FileEditorTool
            from openhands.tools.task_tracker import TaskTrackerTool
            from openhands.tools.preset.default import get_default_tools
            from openhands.workspace import DockerWorkspace
        except Exception as exc:  # pragma: no cover - runtime-specific optional dependencies
            raise OpenHandsUnavailableError(
                f"OpenHands Docker workspace imports failed: {exc}",
                kind="OPENHANDS_DOCKER_IMPORT_FAILED",
            ) from exc

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

        host_workspace = Path(request.workspace_path).resolve()
        host_workspace.mkdir(parents=True, exist_ok=True)
        target_repo_path = Path(request.target_repo).resolve()
        original_repo_before = self._repo_snapshot(target_repo_path)
        workspace_before = self._workspace_snapshot(host_workspace)

        server_image = os.getenv("AGENT_ARMY_OPENHANDS_SERVER_IMAGE", "").strip()
        if not server_image:
            server_image = f"ghcr.io/openhands/agent-server:{sdk_version}-python"
        verify_image = os.getenv("AGENT_ARMY_VERIFICATION_IMAGE", "").strip() or server_image

        source_archive = self._make_source_archive(host_workspace)
        patch_path = host_workspace.parent / f"{request.goal_id}.docker-build.patch"
        if patch_path.exists():
            patch_path.unlink()

        llm = self._build_llm(profile, api_key)
        tools = [
            tool
            for tool in get_default_tools(enable_browser=False)
            if tool.name in {FileEditorTool.name, TaskTrackerTool.name}
        ]
        agent = Agent(llm=llm, tools=tools)
        conversation = None
        builder_runtime: dict[str, Any] = {}
        changed_files: list[str] = []
        verification = _empty_verification("verification not started")
        patch_text = ""
        apply_error: str | None = None

        try:
            with DockerWorkspace(server_image=server_image, platform=_detect_platform()) as remote:
                upload = remote.file_upload(source_archive, "/tmp/agent-army-source.tar.gz")
                if getattr(upload, "success", True) is False:
                    raise RuntimeError(f"Failed to upload staging source to DockerWorkspace: {upload}")

                setup = remote.execute_command(
                    "mkdir -p /workspace && "
                    "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && "
                    "tar -xzf /tmp/agent-army-source.tar.gz -C /workspace && "
                    "git init -q && "
                    "git config user.email agent-army@localhost && "
                    "git config user.name agent-army && "
                    "git add -A && git commit -qm baseline",
                    cwd="/workspace",
                    timeout=120.0,
                )
                if int(getattr(setup, "exit_code", 1)) != 0:
                    raise RuntimeError(f"Docker workspace initialization failed: {getattr(setup, 'stderr', '')}")

                conversation = Conversation(
                    agent,
                    workspace=remote,
                    max_iteration_per_run=max(1, min(self.policy.max_runtime_seconds // 30, 12)),
                )
                conversation.send_message(self._build_task_prompt(request))

                max_run_attempts = max(1, int(self.policy.rate_limit_retry_attempts or 3))
                backoffs = list(self.policy.rate_limit_backoff_seconds or [10.0, 30.0, 60.0])
                for run_attempt in range(max_run_attempts):
                    try:
                        conversation.run()
                        break
                    except Exception as exc:
                        err_text = str(exc).lower()
                        is_rate_limit = any(
                            marker in err_text
                            for marker in ("429", "rate limit", "resource_exhausted", "quota")
                        )
                        if is_rate_limit and run_attempt + 1 < max_run_attempts:
                            delay = backoffs[min(run_attempt, len(backoffs) - 1)] if backoffs else 15.0
                            time.sleep(delay)
                            continue
                        raise

                builder_runtime = self._extract_runtime_stats(llm, conversation)

                changed = remote.execute_command(
                    "git add -N . >/dev/null 2>&1 || true; "
                    "git diff --name-only --diff-filter=ACDMRTUXB -z -- .",
                    cwd="/workspace",
                    timeout=30.0,
                )
                if int(getattr(changed, "exit_code", 1)) != 0:
                    raise RuntimeError(f"Unable to inspect Docker workspace changes: {getattr(changed, 'stderr', '')}")
                changed_files = sorted(
                    item.replace("\\", "/")
                    for item in str(getattr(changed, "stdout", "")).split("\x00")
                    if item
                )

                patch = remote.execute_command(
                    "git diff --binary -- . > /tmp/agent-army-build.patch",
                    cwd="/workspace",
                    timeout=30.0,
                )
                if int(getattr(patch, "exit_code", 1)) != 0:
                    raise RuntimeError(f"Unable to create Docker workspace patch: {getattr(patch, 'stderr', '')}")
                remote.file_download("/tmp/agent-army-build.patch", patch_path)

            if conversation is not None:
                try:
                    conversation.close()
                except Exception:
                    pass

            if patch_path.exists():
                patch_text = patch_path.read_text(encoding="utf-8", errors="replace")

            if patch_text.strip():
                applied = subprocess.run(
                    ["git", "-C", str(host_workspace), "apply", "--binary", "--whitespace=nowarn", str(patch_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if applied.returncode != 0:
                    apply_error = (applied.stderr or applied.stdout or "git apply failed").strip()
                else:
                    verification = _run_docker_verification(
                        host_workspace,
                        request.verification_commands,
                        image=verify_image,
                        timeout=float(self.policy.max_runtime_seconds),
                    )
            else:
                verification = _empty_verification("empty patch")
        finally:
            try:
                Path(source_archive).unlink(missing_ok=True)
            except Exception:
                pass
            if conversation is not None:
                try:
                    conversation.close()
                except Exception:
                    pass

        unauthorized = [item for item in changed_files if item not in request.allowed_files]
        original_repo_after = self._repo_snapshot(target_repo_path)
        original_repo_modified = original_repo_before != original_repo_after

        status = "BUILT_PENDING_REVIEW"
        failure_type = None
        if unauthorized and not request.allow_new_files:
            status = "BUILDER_POLICY_VIOLATION"
            failure_type = f"unauthorized files: {', '.join(unauthorized)}"
        elif apply_error:
            status = "BUILD_FAILED"
            failure_type = f"sandbox patch could not be staged locally: {apply_error}"
        elif verification["status"] != "PASS":
            status = "BUILD_FAILED"
            failure_type = "verification failed"
        elif original_repo_modified:
            status = "BUILD_FAILED"
            failure_type = "original repo modified"
        elif not patch_text.strip():
            status = "BUILD_FAILED"
            failure_type = "empty patch"

        workspace_after = self._workspace_snapshot(host_workspace)
        staged_changed_files = self._workspace_changed_files(workspace_before, workspace_after)
        if staged_changed_files:
            changed_files = sorted(set(changed_files) | set(staged_changed_files))

        return BuilderResult(
            goal_id=request.goal_id,
            status=status,
            failure_type=failure_type,
            recommended_executor="openhands-docker",
            changed_files=changed_files,
            unauthorized_files=unauthorized,
            patch_path=str(patch_path) if patch_path.exists() else None,
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

    def _make_source_archive(self, workspace: Path) -> str:
        handle = tempfile.NamedTemporaryFile(prefix="agent-army-source-", suffix=".tar.gz", delete=False)
        handle.close()
        excluded_dirs = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv"}
        with tarfile.open(handle.name, "w:gz") as archive:
            for path in sorted(workspace.rglob("*")):
                rel = path.relative_to(workspace)
                if any(part in excluded_dirs for part in rel.parts):
                    continue
                if path.name == "build.patch":
                    continue
                archive.add(path, arcname=str(rel).replace("\\", "/"), recursive=False)
        return handle.name


def _detect_platform() -> str:
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        return "linux/arm64"
    return "linux/amd64"


def _empty_verification(reason: str) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "exit_code": 1,
        "command": "none",
        "stdout": "",
        "stderr": reason,
        "duration_ms": 0,
        "failure_code": "SANDBOX_VERIFICATION_FAILED",
        "command_results": [],
        "reason": reason,
    }


def _run_docker_verification(
    workspace: Path,
    commands: list[str],
    *,
    image: str,
    timeout: float,
) -> dict[str, Any]:
    if not commands:
        return {
            "status": "PASS",
            "exit_code": 0,
            "command": "none",
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "failure_code": None,
            "command_results": [],
            "reason": "",
        }

    results: list[dict[str, Any]] = []
    total_ms = 0
    for raw in commands:
        command = str(raw).strip()
        if not command:
            continue
        container_name = f"agent-army-verify-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--memory",
            os.getenv("AGENT_ARMY_VERIFY_MEMORY", "2g"),
            "--cpus",
            os.getenv("AGENT_ARMY_VERIFY_CPUS", "2"),
            "--pids-limit",
            os.getenv("AGENT_ARMY_VERIFY_PIDS", "256"),
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "-v",
            f"{workspace}:/workspace",
            "-w",
            "/workspace",
            "--entrypoint",
            "/bin/sh",
            image,
            "-lc",
            command,
        ]
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout),
                check=False,
                stdin=subprocess.DEVNULL,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            total_ms += elapsed
            item = {
                "command": command,
                "argv": ["docker", "<isolated-container>", command],
                "status": "PASS" if proc.returncode == 0 else "FAIL",
                "exit_code": proc.returncode,
                "stdout": _clip(proc.stdout),
                "stderr": _clip(proc.stderr),
                "duration_ms": elapsed,
                "failure_code": None if proc.returncode == 0 else "NONZERO_EXIT",
                "sandbox": "docker-network-none",
            }
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True, check=False)
            elapsed = int((time.perf_counter() - started) * 1000)
            total_ms += elapsed
            item = {
                "command": command,
                "argv": ["docker", "<isolated-container>", command],
                "status": "FAIL",
                "exit_code": 124,
                "stdout": _clip(exc.stdout or ""),
                "stderr": _clip(exc.stderr or "") + f"\nVerification timed out after {timeout}s",
                "duration_ms": elapsed,
                "failure_code": "VERIFICATION_TIMEOUT",
                "sandbox": "docker-network-none",
            }
        results.append(item)
        if item["status"] != "PASS":
            return {
                "status": "FAIL",
                "exit_code": item["exit_code"],
                "command": command,
                "stdout": item["stdout"],
                "stderr": item["stderr"],
                "duration_ms": total_ms,
                "failure_code": item["failure_code"],
                "command_results": results,
                "reason": item["failure_code"] or "verification failed",
                "sandbox": "docker-network-none",
            }

    return {
        "status": "PASS",
        "exit_code": 0,
        "command": results[0]["command"] if results else "none",
        "stdout": results[-1]["stdout"] if results else "",
        "stderr": results[-1]["stderr"] if results else "",
        "duration_ms": total_ms,
        "failure_code": None,
        "command_results": results,
        "reason": "",
        "sandbox": "docker-network-none",
    }


def _clip(text: str, limit: int = 8192) -> str:
    value = text or ""
    return value if len(value) <= limit else value[:limit] + "..."
