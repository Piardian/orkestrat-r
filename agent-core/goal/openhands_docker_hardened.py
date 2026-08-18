from __future__ import annotations

from pathlib import Path, PurePosixPath
import shlex
import subprocess

from .builder import BuilderRequest, BuilderResult
from .openhands_docker_adapter import DockerOpenHandsBuilderAdapter


_RUNTIME_DIR_CANDIDATES = (
    ".openhands/",
    "conversations/",
    "bash_events/",
)


def _normalize_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _tracked_paths(repo: str | Path) -> set[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return set()
    return {_normalize_path(item) for item in proc.stdout.split("\x00") if item}


def runtime_exclude_patterns(target_repo: str | Path, allowed_files: list[str]) -> tuple[str, ...]:
    """Return safe Git excludes for OpenHands runtime-only metadata.

    Directory excludes are omitted if the project already tracks or explicitly allows
    a path below that directory. This keeps runtime filtering fail-closed for projects
    that legitimately use names such as ``conversations/``.
    """

    tracked = _tracked_paths(target_repo)
    allowed = {_normalize_path(item) for item in allowed_files}
    protected = tracked | allowed
    patterns: list[str] = []

    for candidate in _RUNTIME_DIR_CANDIDATES:
        root = candidate.rstrip("/")
        in_use = any(path == root or path.startswith(root + "/") for path in protected)
        if not in_use:
            patterns.append(candidate)

    if not any("__pycache__" in PurePosixPath(path).parts for path in protected):
        patterns.append("__pycache__/")
    if not any(path.endswith(".pyc") for path in protected):
        patterns.append("*.pyc")
    return tuple(patterns)


def _git_exclude_command(patterns: tuple[str, ...]) -> str:
    if not patterns:
        return "true"
    quoted = " ".join(shlex.quote(item) for item in patterns)
    return f"printf '%s\\n' {quoted} >> .git/info/exclude"


def _matches_runtime_metadata(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = _normalize_path(path)
    parts = PurePosixPath(normalized).parts
    for pattern in patterns:
        if pattern == "*.pyc":
            if normalized.endswith(".pyc"):
                return True
            continue
        root = pattern.rstrip("/")
        if root and root in parts:
            return True
    return False


class HardenedDockerOpenHandsBuilderAdapter(DockerOpenHandsBuilderAdapter):
    """Docker adapter that keeps OpenHands runtime metadata out of the build patch."""

    def _execute_live(self, request: BuilderRequest, *, sdk_version: str, tools_version: str) -> BuilderResult:
        patterns = runtime_exclude_patterns(request.target_repo, request.allowed_files)
        self._active_runtime_excludes = patterns

        try:
            import openhands.workspace as workspace_module
        except Exception:
            self._active_runtime_excludes = ()
            return super()._execute_live(request, sdk_version=sdk_version, tools_version=tools_version)

        original_workspace = workspace_module.DockerWorkspace
        exclude_command = _git_exclude_command(patterns)

        class RuntimeFilteredDockerWorkspace(original_workspace):
            def execute_command(inner_self, command, *args, **kwargs):
                if patterns and "git init -q && " in str(command):
                    command = str(command).replace(
                        "git init -q && ",
                        f"git init -q && {exclude_command} && ",
                        1,
                    )
                return super().execute_command(command, *args, **kwargs)

        workspace_module.DockerWorkspace = RuntimeFilteredDockerWorkspace
        try:
            return super()._execute_live(request, sdk_version=sdk_version, tools_version=tools_version)
        finally:
            workspace_module.DockerWorkspace = original_workspace
            self._active_runtime_excludes = ()

    def _workspace_changed_files(self, before: dict[str, str], after: dict[str, str]) -> list[str]:
        changed = super()._workspace_changed_files(before, after)
        patterns = tuple(getattr(self, "_active_runtime_excludes", ()))
        if not patterns:
            return changed
        return [item for item in changed if not _matches_runtime_metadata(item, patterns)]
