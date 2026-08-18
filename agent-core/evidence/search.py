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
            lines = [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]
            filtered = []
            for line in lines:
                parts = tuple(line.split("/"))
                if not _is_excluded_path(parts, active_excludes) and not is_secret_path(line):
                    if not any(fnmatch.fnmatch(Path(line).name.lower(), g.lower()) for g in DEFAULT_FILE_GLOBS):
                        filtered.append(line)
            filtered.sort(key=lambda p: (_rank_file(p), p))
            return filtered[:max_results]
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
            lines = [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]
            filtered = []
            for line in lines:
                parts = tuple(line.split("/"))
                if not _is_excluded_path(parts, active_excludes) and not is_secret_path(line):
                    if not any(fnmatch.fnmatch(Path(line).name.lower(), g.lower()) for g in DEFAULT_FILE_GLOBS):
                        filtered.append(line)
            filtered.sort(key=lambda p: (_rank_file(p), p))
            return {
                "query": term,
                "files": filtered[:max_results],
                "truncated": len(filtered) > max_results,
                "exit_code": completed.returncode,
            }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return _python_search_fallback(repo, term, max_results, excludes)


def _python_files_fallback(repo: Path, max_results: int, excludes: list[str] | None = None) -> list[str]:
    active_excludes = excludes or DEFAULT_EXCLUDES
    collected = []
    for path in sorted(repo.rglob("*")):
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
        if any(fnmatch.fnmatch(path.name.lower(), g.lower()) for g in DEFAULT_FILE_GLOBS):
            continue
        collected.append(rel_str)

    collected.sort(key=lambda p: (_rank_file(p), p))
    return collected[:max_results]


def _python_search_fallback(repo: Path, term: str, max_results: int, excludes: list[str] | None = None) -> dict:
    files = _python_files_fallback(repo, max_results * 10, excludes)
    matched = []
    for rel_path in files:
        file_path = repo / rel_path
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if term in content:
                matched.append(rel_path)
        except Exception:
            continue

    matched.sort(key=lambda p: (_rank_file(p), p))
    return {
        "query": term,
        "files": matched[:max_results],
        "truncated": len(matched) > max_results,
        "exit_code": 0,
    }
