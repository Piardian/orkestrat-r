from __future__ import annotations

import fnmatch
from pathlib import Path
import subprocess

from .sanitizer import is_secret_path


DEFAULT_EXCLUDES = [
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "data",
    "logs",
    "reports",
    "artifacts",
    "evidence",
    "runtime-logs",
    "test-output",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "chat_history",
    ".ai_chat_history",
    "daily_logs",
    "research_runs",
    "output*",
    "external_review_package*",
    "coverage",
]

DEFAULT_FILE_GLOBS = [
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.log",
    "*.lock",
    "package-lock.json",
    "jest-results*.json",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.webp",
    "*.pdf",
    "*.zip",
    "*.tar*",
    "*.gz",
    "*.parquet",
    "*.pkl",
    "*.bin",
    "*.exe",
    "*.dll",
    "*.so",
    "*.dylib",
    "*.h5",
    "*.npy",
    "*.npz",
    "*.jsonl",
]

PREFERRED_SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".txt",
    ".sh",
    ".bat",
    ".html",
    ".css",
}


def _matches_pattern(part: str, pattern: str) -> bool:
    return fnmatch.fnmatch(part.lower(), pattern.lower())


def _is_excluded_path(rel_parts: tuple[str, ...], excludes: list[str]) -> bool:
    for part in rel_parts:
        for excl in excludes:
            if _matches_pattern(part, excl):
                return True
    return False


def _rank_file(rel_str: str) -> int:
    name = Path(rel_str).name.lower()
    suffix = Path(rel_str).suffix.lower()

    if name in {"main.py", "app.py", "cli.py", "index.py", "index.js", "index.ts"}:
        return 0
    if name in {"pyproject.toml", "requirements.txt", "package.json", "setup.py", "settings.py"}:
        return 1
    if suffix == ".py":
        return 2
    if suffix in {".js", ".ts", ".tsx", ".jsx"}:
        return 3
    if suffix in {".json", ".yaml", ".yml", ".toml"}:
        return 4
    if suffix in {".md", ".txt"}:
        return 5
    return 10


def _filter_candidates(repo: Path, lines: list[str], active_excludes: list[str]) -> list[str]:
    filtered: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        line = str(raw).strip().replace("\\", "/")
        if not line or line in seen:
            continue
        seen.add(line)
        parts = tuple(part for part in line.split("/") if part)
        if _is_excluded_path(parts, active_excludes) or is_secret_path(line):
            continue
        if any(fnmatch.fnmatch(Path(line).name.lower(), glob.lower()) for glob in DEFAULT_FILE_GLOBS):
            continue
        if not (repo / line).is_file():
            continue
        filtered.append(line)
    filtered.sort(key=lambda p: (_rank_file(p), p))
    return filtered


def _git_files_fallback(
    repo: Path,
    max_results: int | None,
    excludes: list[str] | None = None,
) -> list[str] | None:
    """List repository files through Git when ripgrep cannot be executed.

    ``git ls-files`` is substantially cheaper than a recursive Python walk on
    large repositories and is available anywhere the goal pipeline can perform
    its normal Git safety checks. Tracked and non-ignored untracked files are
    included so the fallback remains useful during local development.
    """

    active_excludes = excludes or DEFAULT_EXCLUDES
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"],
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=5,
            shell=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None

    filtered = _filter_candidates(repo, completed.stdout.split("\0"), active_excludes)
    if max_results is None:
        return filtered
    return filtered[:max_results]


def rg_files(repo: Path, max_results: int, excludes: list[str] | None = None) -> list[str]:
    active_excludes = excludes or DEFAULT_EXCLUDES
    args = ["rg", "--files"]
    for item in active_excludes:
        args.extend(["--glob", f"!{item}/**", "--glob", f"!{item}"])
    for item in DEFAULT_FILE_GLOBS:
        args.extend(["--glob", f"!{item}"])
    try:
        completed = subprocess.run(args, cwd=repo, text=True, capture_output=True, timeout=5, shell=False, stdin=subprocess.DEVNULL)
        if completed.returncode == 0:
            filtered = _filter_candidates(repo, completed.stdout.splitlines(), active_excludes)
            return filtered[:max_results]
    except (OSError, subprocess.TimeoutExpired):
        # Windows can raise PermissionError/OSError when rg.exe is present but
        # blocked by policy/ACLs. Treat it exactly like an unavailable binary.
        pass
    return _python_files_fallback(repo, max_results, excludes)


def search_files(repo: Path, term: str, max_results: int, excludes: list[str] | None = None) -> dict:
    active_excludes = excludes or DEFAULT_EXCLUDES
    args = ["rg", "-l", "--fixed-strings", term]
    for item in active_excludes:
        args.extend(["--glob", f"!{item}/**", "--glob", f"!{item}"])
    for item in DEFAULT_FILE_GLOBS:
        args.extend(["--glob", f"!{item}"])
    try:
        completed = subprocess.run(args, cwd=repo, text=True, capture_output=True, timeout=5, shell=False, stdin=subprocess.DEVNULL)
        if completed.returncode in {0, 1}:
            filtered = _filter_candidates(repo, completed.stdout.splitlines(), active_excludes)
            return {
                "query": term,
                "files": filtered[:max_results],
                "truncated": len(filtered) > max_results,
                "exit_code": completed.returncode,
            }
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _python_search_fallback(repo, term, max_results, excludes)


def _walk_files_fallback(repo: Path, excludes: list[str] | None = None) -> list[str]:
    active_excludes = excludes or DEFAULT_EXCLUDES
    collected: list[str] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(repo)
        except Exception:
            continue
        if _is_excluded_path(rel.parts, active_excludes):
            continue
        rel_str = str(rel).replace("\\", "/")
        if is_secret_path(rel_str):
            continue
        if any(fnmatch.fnmatch(path.name.lower(), glob.lower()) for glob in DEFAULT_FILE_GLOBS):
            continue
        collected.append(rel_str)

    collected.sort(key=lambda p: (_rank_file(p), p))
    return collected


def _python_files_fallback(repo: Path, max_results: int, excludes: list[str] | None = None) -> list[str]:
    git_files = _git_files_fallback(repo, max_results, excludes)
    if git_files is not None:
        return git_files
    return _walk_files_fallback(repo, excludes)[:max_results]


def _python_search_fallback(repo: Path, term: str, max_results: int, excludes: list[str] | None = None) -> dict:
    files = _git_files_fallback(repo, None, excludes)
    if files is None:
        files = _walk_files_fallback(repo, excludes)

    matched: list[str] = []
    truncated = False
    for rel_path in files:
        file_path = repo / rel_path
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if term in content:
                matched.append(rel_path)
                if len(matched) > max_results:
                    truncated = True
                    break
        except (OSError, UnicodeError):
            continue

    matched.sort(key=lambda p: (_rank_file(p), p))
    return {
        "query": term,
        "files": matched[:max_results],
        "truncated": truncated,
        "exit_code": 0,
    }
