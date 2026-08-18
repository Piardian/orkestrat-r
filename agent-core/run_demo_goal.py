from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import tempfile
from typing import Any

from goal.openhands_adapter import OpenHandsBuilderAdapter, OpenHandsUnavailableError


_PROTECTED_PARTS = {".git", ".ssh", ".aws", ".azure", ".kube", ".gnupg"}
_PROTECTED_FILES = {
    ".npmrc",
    ".pypirc",
    ".netrc",
    "_netrc",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "application_default_credentials.json",
}
_BULKY_PARTS = {".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Direct OpenHands demo runner. Bypasses the normal planner/reviewer/"
            "allowed-files/verification/apply pipeline and runs one natural-language "
            "goal in an isolated Docker workspace, then mirrors the selected workspace "
            "back to the host."
        )
    )
    parser.add_argument("--workspace", required=True, help="Host folder exposed to the demo agent.")
    parser.add_argument("--task", required=True, help="Natural-language goal for OpenHands.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result.")
    args = parser.parse_args()

    try:
        result = run_demo_goal(args.workspace, args.task)
    except Exception as exc:
        print(f"DEMO_GOAL_FAILED: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("DEMO GOAL COMPLETED")
        print(f"Workspace: {result['workspace']}")
        print(f"Tools: {', '.join(result['tools'])}")
        changed = result["changed_files"]
        print("Changed files: " + (", ".join(changed) if changed else "NONE"))
        if result.get("assistant_result"):
            print("\nOpenHands result:")
            print(result["assistant_result"])
    return 0


def run_demo_goal(workspace: str | Path, task: str) -> dict[str, Any]:
    task_text = str(task or "").strip()
    if not task_text:
        raise ValueError("--task cannot be blank")

    host_workspace = Path(workspace).expanduser().resolve()
    if host_workspace == Path(host_workspace.anchor):
        raise ValueError("Filesystem root cannot be used as the demo workspace; choose a folder such as Desktop or a demo folder.")
    host_workspace.mkdir(parents=True, exist_ok=True)

    if shutil.which("docker") is None:
        raise RuntimeError("Docker is required for demo mode.")

    try:
        from dotenv import load_dotenv

        dotenv_path = Path(__file__).resolve().parent / ".env"
        if dotenv_path.exists():
            load_dotenv(dotenv_path=dotenv_path, override=False)
    except Exception:
        pass

    try:
        from openhands.sdk.agent import Agent
        from openhands.sdk.conversation import Conversation
        from openhands.sdk.event import LLMConvertibleEvent
        from openhands.sdk.tool.spec import Tool
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.task_tracker import TaskTrackerTool
        from openhands.tools.terminal import TerminalTool
        from openhands.workspace import DockerWorkspace
    except Exception as exc:
        raise OpenHandsUnavailableError(
            f"OpenHands demo imports failed: {exc}",
            kind="OPENHANDS_DEMO_IMPORT_FAILED",
        ) from exc

    adapter = OpenHandsBuilderAdapter()
    profile = adapter._load_profile()
    secret_env = profile.get("secret_env")
    api_key = os.getenv(secret_env) if secret_env else None
    if secret_env and not api_key:
        raise OpenHandsUnavailableError(
            f"Missing configured secret environment variable: {secret_env}",
            kind="OPENHANDS_SECRET_MISSING",
        )
    llm = adapter._build_llm(profile, api_key)

    sdk_version = importlib.metadata.version("openhands-sdk")
    server_image = os.getenv("AGENT_ARMY_OPENHANDS_SERVER_IMAGE", "").strip()
    if not server_image:
        server_image = f"ghcr.io/openhands/agent-server:{sdk_version}-python"

    before = _snapshot(host_workspace)
    source_archive = _make_source_archive(host_workspace)
    result_handle = tempfile.NamedTemporaryFile(prefix="agent-demo-result-", suffix=".tar.gz", delete=False)
    result_handle.close()
    result_archive = Path(result_handle.name)
    messages: list[Any] = []
    conversation = None

    def on_event(event: Any) -> None:
        try:
            if isinstance(event, LLMConvertibleEvent):
                messages.append(event.to_llm_message())
        except Exception:
            return

    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ]
    tool_names = [TerminalTool.name, FileEditorTool.name, TaskTrackerTool.name]
    agent = Agent(llm=llm, tools=tools)

    prompt = f"""You are running in AGENT ARMY DEMO MODE inside /workspace.

User goal:
{task_text}

Demo-mode behavior:
- Execute the goal directly. There is no planner, reviewer, allowed-files list, verification gate, or transactional apply step.
- You may use the terminal and file editor freely inside /workspace.
- /workspace is the user's selected host-visible demo folder. You may inspect, create, edit, rename, move, or delete items there as needed for the goal.
- Do not assume this is a git repository.
- When the goal is read-only (for example, finding a folder), do not invent a file change; report the answer clearly.
- Finish with a concise statement of what you found or changed.
"""

    try:
        with DockerWorkspace(server_image=server_image, platform=_detect_platform()) as remote:
            upload = remote.file_upload(source_archive, "/tmp/agent-demo-source.tar.gz")
            if getattr(upload, "success", True) is False:
                raise RuntimeError(f"Failed to upload demo workspace: {upload}")

            setup = remote.execute_command(
                "mkdir -p /workspace && "
                "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && "
                "tar -xzf /tmp/agent-demo-source.tar.gz -C /workspace",
                cwd="/workspace",
                timeout=180.0,
            )
            if int(getattr(setup, "exit_code", 1)) != 0:
                raise RuntimeError(f"Demo workspace initialization failed: {getattr(setup, 'stderr', '')}")

            conversation = Conversation(
                agent,
                callbacks=[on_event],
                workspace=remote,
                max_iteration_per_run=24,
            )
            conversation.send_message(prompt)
            conversation.run()

            packed = remote.execute_command(
                "tar -czf /tmp/agent-demo-result.tar.gz -C /workspace .",
                cwd="/workspace",
                timeout=180.0,
            )
            if int(getattr(packed, "exit_code", 1)) != 0:
                raise RuntimeError(f"Unable to package demo result: {getattr(packed, 'stderr', '')}")
            remote.file_download("/tmp/agent-demo-result.tar.gz", result_archive)
    finally:
        if conversation is not None:
            try:
                conversation.close()
            except Exception:
                pass
        try:
            Path(source_archive).unlink(missing_ok=True)
        except Exception:
            pass

    try:
        with tempfile.TemporaryDirectory(prefix="agent-demo-extract-") as tmp:
            extracted = Path(tmp)
            _safe_extract(result_archive, extracted)
            _sync_from_extracted(extracted, host_workspace)
    finally:
        result_archive.unlink(missing_ok=True)

    after = _snapshot(host_workspace)
    changed_files = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    assistant_result = _extract_assistant_result(messages)

    return {
        "status": "COMPLETED",
        "workspace": str(host_workspace),
        "task": task_text,
        "tools": tool_names,
        "changed_files": changed_files,
        "assistant_result": assistant_result,
        "server_image": server_image,
    }


def _make_source_archive(workspace: Path) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="agent-demo-source-", suffix=".tar.gz", delete=False)
    handle.close()
    with tarfile.open(handle.name, "w:gz") as archive:
        for path in sorted(workspace.rglob("*")):
            rel = path.relative_to(workspace)
            if _skip_source(rel):
                continue
            archive.add(path, arcname=str(rel).replace("\\", "/"), recursive=False)
    return handle.name


def _skip_source(rel: Path) -> bool:
    lowered = [part.lower() for part in rel.parts]
    if any(part in _BULKY_PARTS for part in lowered):
        return True
    return _is_protected_relative(rel)


def _is_protected_relative(rel: Path) -> bool:
    lowered = [part.lower() for part in rel.parts]
    if any(part in _PROTECTED_PARTS for part in lowered):
        return True
    if not lowered:
        return False
    name = lowered[-1]
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _PROTECTED_FILES:
        return True
    if len(lowered) >= 2 and lowered[-2:] == ["docker", "config.json"]:
        return True
    return False


def _safe_extract(archive_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            raw = member.name.replace("\\", "/")
            if raw in {"", ".", "./"}:
                continue
            while raw.startswith("./"):
                raw = raw[2:]
            rel = Path(raw)
            if rel.is_absolute() or ".." in rel.parts or _is_protected_relative(rel):
                continue
            target = (destination / rel).resolve()
            if destination != target and destination not in target.parents:
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)


def _sync_from_extracted(source: Path, target: Path) -> None:
    source_entries = {
        Path(str(path.relative_to(source)).replace("\\", "/"))
        for path in source.rglob("*")
        if not _is_protected_relative(path.relative_to(source))
    }

    target_entries = sorted(
        (
            path
            for path in target.rglob("*")
            if not _is_protected_relative(path.relative_to(target)) and not _skip_source(path.relative_to(target))
        ),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for path in target_entries:
        rel = Path(str(path.relative_to(target)).replace("\\", "/"))
        if rel in source_entries:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass

    for path in sorted(source.rglob("*"), key=lambda p: len(p.parts)):
        rel = path.relative_to(source)
        if _is_protected_relative(rel):
            continue
        destination = target / rel
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def _snapshot(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        if _is_protected_relative(rel) or _skip_source(rel):
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        result[str(rel).replace("\\", "/")] = digest
    return result


def _extract_assistant_result(messages: list[Any]) -> str:
    for message in reversed(messages):
        role = getattr(message, "role", None)
        role_value = getattr(role, "value", role)
        if str(role_value).lower() != "assistant":
            continue
        content = getattr(message, "content", None)
        text = _content_to_text(content)
        if text:
            return text
    return ""


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts).strip()
    text = getattr(content, "text", None)
    return str(text).strip() if text else str(content).strip()


def _detect_platform() -> str:
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        return "linux/arm64"
    return "linux/amd64"


if __name__ == "__main__":
    raise SystemExit(main())
