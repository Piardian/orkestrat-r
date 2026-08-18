from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
import uuid


def run_docker_verification_suite(
    commands: list[Any],
    source_workspace: Path | str,
    *,
    timeout: float = 60.0,
    image: str | None = None,
) -> dict[str, Any]:
    """Run verification in a disposable, networkless Docker container.

    The source tree is copied to a temporary directory first. The container never
    receives the user's environment or API keys and never gets a writable mount of
    the real repository/worktree.
    """
    if shutil.which("docker") is None:
        return _failure("DOCKER_NOT_INSTALLED", "Docker is required for sandboxed verification")

    source = Path(source_workspace).resolve()
    if not source.exists() or not source.is_dir():
        return _failure("CWD_NOT_FOUND", f"Verification working directory not found: {source}")

    verify_image = (
        image
        or os.getenv("AGENT_ARMY_VERIFICATION_IMAGE", "").strip()
        or "ghcr.io/openhands/agent-server:1.42.1-python"
    )
    sandbox_root = Path(tempfile.mkdtemp(prefix="agent-army-verify-copy-"))
    sandbox_workspace = sandbox_root / "workspace"
    try:
        shutil.copytree(
            source,
            sandbox_workspace,
            symlinks=False,
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".venv",
                "*.pyc",
            ),
        )
        return _run_commands(commands, sandbox_workspace, verify_image, timeout)
    except Exception as exc:
        return _failure("SANDBOX_PREP_FAILED", f"Unable to prepare Docker verification sandbox: {exc}")
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)


def _run_commands(commands: list[Any], workspace: Path, image: str, timeout: float) -> dict[str, Any]:
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
            "sandbox": "docker-network-none-copy",
        }

    results: list[dict[str, Any]] = []
    total_ms = 0
    for raw in commands:
        command = _command_text(raw)
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
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,nosuid,size=256m",
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
                timeout=max(1.0, float(timeout)),
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
                "sandbox": "docker-network-none-copy",
            }
        except subprocess.TimeoutExpired as exc:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            total_ms += elapsed
            item = {
                "command": command,
                "argv": ["docker", "<isolated-container>", command],
                "status": "FAIL",
                "exit_code": 124,
                "stdout": _clip(_as_text(exc.stdout)),
                "stderr": (_clip(_as_text(exc.stderr)) + f"\nVerification timed out after {timeout}s").strip(),
                "duration_ms": elapsed,
                "failure_code": "VERIFICATION_TIMEOUT",
                "sandbox": "docker-network-none-copy",
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
                "sandbox": "docker-network-none-copy",
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
        "sandbox": "docker-network-none-copy",
    }


def _command_text(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        import shlex

        return " ".join(shlex.quote(str(item)) for item in raw if str(item).strip())
    return str(raw or "").strip()


def _failure(code: str, message: str) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "exit_code": 126,
        "command": "none",
        "stdout": "",
        "stderr": message,
        "duration_ms": 0,
        "failure_code": code,
        "command_results": [],
        "reason": code,
        "sandbox": "docker-network-none-copy",
    }


def _clip(text: str, limit: int = 8192) -> str:
    value = text or ""
    return value if len(value) <= limit else value[:limit] + "..."


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
