from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from typing import Any

from goal.openhands_adapter import OpenHandsBuilderAdapter, OpenHandsUnavailableError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Direct OpenHands demo runner using a Docker bind mount instead of copying the workspace."
    )
    parser.add_argument("--workspace", required=True, help="Host folder mounted at /workspace.")
    parser.add_argument("--task", required=True, help="Natural-language goal for OpenHands.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = run_demo_goal(args.workspace, args.task, quiet=args.json)
    except KeyboardInterrupt:
        print("DEMO_GOAL_CANCELLED", file=sys.stderr)
        return 130
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


def run_demo_goal(workspace: str | Path, task: str, *, quiet: bool = False) -> dict[str, Any]:
    task_text = str(task or "").strip()
    if not task_text:
        raise ValueError("--task cannot be blank")

    host_workspace = Path(workspace).expanduser().resolve()
    if host_workspace == Path(host_workspace.anchor):
        raise ValueError("Filesystem root cannot be used as the demo workspace.")
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

    before = _fast_snapshot(host_workspace)
    messages: list[Any] = []

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
- Execute the user's goal directly.
- You may use terminal and file editing tools inside /workspace.
- /workspace is directly mounted from the user's selected host folder, so edits are immediately visible on the host.
- Do not assume this is a git repository.
- If the goal is read-only, report what you found without making unnecessary changes.
- Finish with a concise statement of what you found or changed.
"""

    volume = _docker_volume_spec(host_workspace)
    _progress(quiet, f"[demo] Workspace: {host_workspace}")
    _progress(quiet, "[demo] Starting OpenHands Docker workspace...")

    conversation = None
    try:
        with DockerWorkspace(
            server_image=server_image,
            platform=_detect_platform(),
            volumes=[volume],
            working_dir="/workspace",
        ) as remote:
            _progress(quiet, "[demo] Docker workspace ready. Running goal...")
            conversation = Conversation(
                agent,
                callbacks=[on_event],
                workspace=remote,
                max_iteration_per_run=24,
            )
            conversation.send_message(prompt)
            conversation.run()
            _progress(quiet, "[demo] OpenHands finished. Reading result...")
    finally:
        if conversation is not None:
            try:
                conversation.close()
            except Exception:
                pass

    after = _fast_snapshot(host_workspace)
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
        "volume": volume,
    }


def _docker_volume_spec(workspace: Path) -> str:
    return f"{workspace.resolve()}:/workspace"


def _fast_snapshot(workspace: Path) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    for path in workspace.rglob("*"):
        try:
            rel = str(path.relative_to(workspace)).replace("\\", "/")
            stat = path.stat()
            kind = "dir" if path.is_dir() else "file" if path.is_file() else "other"
            result[rel] = (kind, int(stat.st_size), int(stat.st_mtime_ns))
        except OSError:
            continue
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


def _progress(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
