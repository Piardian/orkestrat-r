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
    return run_docker_commands_in_workspace(
        commands,
        workspace,
        image=image,
        timeout=timeout,
        sandbox="docker-network-none-copy",
        cap_drop_all=True,
        tmpfs_options="rw,nosuid,size=256m",
    )


def run_docker_commands_in_workspace(
    commands: list[Any],
    workspace: Path | str,
    *,
    image: str,
    timeout: float,
    sandbox: str = "docker-network-none",
    cap_drop_all: bool = False,
    tmpfs_options: str = "rw,noexec,nosuid,size=256m",
) -> dict[str, Any]:
    """Run a verification sequence in one disposable Docker container.

    Verification commands are intentionally executed with separate ``docker
    exec`` calls while the container remains alive. This preserves background
    processes (for example, a local HTTP server) across commands without
    combining the whole suite into one opaque shell script. Foreground commands
    are sent over stdin so cleanup probes such as ``pkill -f <pattern>`` cannot
    match the verification shell's own argv. A command ending in ``&`` becomes
    a detached Docker exec so its file descriptors cannot keep the launch call
    open and its process remains available to later probes.
    """
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
            "sandbox": sandbox,
        }

    workspace_path = Path(workspace).resolve()
    results: list[dict[str, Any]] = []
    total_ms = 0
    container_name = f"agent-army-verify-{uuid.uuid4().hex[:12]}"
    start_argv = [
        "docker",
        "run",
        "-d",
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
    ]
    if cap_drop_all:
        start_argv.extend(["--cap-drop", "ALL"])
    start_argv.extend(
        [
            "--tmpfs",
            f"/tmp:{tmpfs_options}",
            "-v",
            f"{workspace_path}:/workspace",
            "-w",
            "/workspace",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            "while :; do sleep 3600; done",
        ]
    )

    setup_started = time.perf_counter()
    try:
        try:
            setup = subprocess.run(
                start_argv,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout)),
                check=False,
                stdin=subprocess.DEVNULL,
            )
            total_ms += int((time.perf_counter() - setup_started) * 1000)
        except subprocess.TimeoutExpired as exc:
            total_ms += int((time.perf_counter() - setup_started) * 1000)
            return {
                "status": "FAIL",
                "exit_code": 124,
                "command": "docker run verification sandbox",
                "stdout": _clip(_as_text(exc.stdout)),
                "stderr": (
                    _clip(_as_text(exc.stderr))
                    + f"\nVerification sandbox startup timed out after {timeout}s"
                ).strip(),
                "duration_ms": total_ms,
                "failure_code": "SANDBOX_START_TIMEOUT",
                "command_results": [],
                "reason": "SANDBOX_START_TIMEOUT",
                "sandbox": sandbox,
            }

        if setup.returncode != 0:
            return {
                "status": "FAIL",
                "exit_code": setup.returncode,
                "command": "docker run verification sandbox",
                "stdout": _clip(setup.stdout),
                "stderr": _clip(setup.stderr),
                "duration_ms": total_ms,
                "failure_code": "SANDBOX_START_FAILED",
                "command_results": [],
                "reason": "SANDBOX_START_FAILED",
                "sandbox": sandbox,
            }
        for raw in commands:
            command = _command_text(raw)
            if not command:
                continue
            detached = command.rstrip().endswith("&")
            if detached:
                detached_command = command.rstrip()[:-1].rstrip()
                exec_argv = [
                    "docker",
                    "exec",
                    "-d",
                    container_name,
                    "/bin/sh",
                    "-lc",
                    detached_command,
                ]
                command_input = None
            else:
                exec_argv = ["docker", "exec", "-i", container_name, "/bin/sh"]
                command_input = f"{command}\n"
            started = time.perf_counter()
            try:
                proc = subprocess.run(
                    exec_argv,
                    # Send an explicit LF byte stream. On Windows, subprocess
                    # text mode translates ``\n`` to CRLF; the Linux shell then
                    # leaves ``\r`` attached to the final argument (for example
                    # npm receives ``test\r`` instead of ``test``).
                    input=command_input.encode("utf-8") if command_input is not None else None,
                    capture_output=True,
                    timeout=max(1.0, float(timeout)),
                    check=False,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                total_ms += elapsed
                item = {
                    "command": command,
                    "argv": [
                        "docker",
                        "exec",
                        "<isolated-container>",
                        "<detached-background-command>" if detached else "<stdin-command>",
                    ],
                    "status": "PASS" if proc.returncode == 0 else "FAIL",
                    "exit_code": proc.returncode,
                    "stdout": _clip(_as_text(proc.stdout)),
                    "stderr": _clip(_as_text(proc.stderr)),
                    "duration_ms": elapsed,
                    "failure_code": None if proc.returncode == 0 else "NONZERO_EXIT",
                    "sandbox": sandbox,
                }
            except subprocess.TimeoutExpired as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                total_ms += elapsed
                item = {
                    "command": command,
                    "argv": [
                        "docker",
                        "exec",
                        "<isolated-container>",
                        "<detached-background-command>" if detached else "<stdin-command>",
                    ],
                    "status": "FAIL",
                    "exit_code": 124,
                    "stdout": _clip(_as_text(exc.stdout)),
                    "stderr": (
                        _clip(_as_text(exc.stderr))
                        + f"\nVerification timed out after {timeout}s"
                    ).strip(),
                    "duration_ms": elapsed,
                    "failure_code": "VERIFICATION_TIMEOUT",
                    "sandbox": sandbox,
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
                    "sandbox": sandbox,
                }

            if detached:
                grace = max(
                    0.0,
                    float(os.getenv("AGENT_ARMY_VERIFY_BACKGROUND_GRACE_SECONDS", "0.5")),
                )
                if grace:
                    time.sleep(grace)
                    total_ms += int(grace * 1000)

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
            "sandbox": sandbox,
        }
    finally:
        # Cleanup is best-effort even when startup timed out after Docker had
        # already created the named container.
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


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
