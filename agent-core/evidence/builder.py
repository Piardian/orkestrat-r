from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import ast
import json
import re

from .files import read_line_range
from .git_tools import git_summary
from .search import rg_files, search_files, _python_files_fallback, DEFAULT_EXCLUDES, DEFAULT_FILE_GLOBS
from .test_tools import run_test_command


@dataclass(frozen=True)
class EvidenceLimits:
    max_search_results: int = 20
    max_files: int = 16
    max_lines_per_file: int = 150
    max_command_output_lines: int = 50
    max_test_output_lines: int = 50
    max_chars_per_file: int = 24000


class EvidenceBuilder:
    def __init__(self, repo: str | Path, plan: dict[str, Any]) -> None:
        self.repo = Path(repo).resolve()
        if not self.repo.exists():
            raise FileNotFoundError(f"Repository not found: {self.repo}")
        self.plan = plan
        self.limits = EvidenceLimits(
            max_search_results=int(plan.get("max_search_results", 20)),
            max_files=max(int(plan.get("max_files", 16)), 12),
            max_lines_per_file=max(int(plan.get("max_lines_per_file", 150)), 120),
            max_command_output_lines=int(plan.get("max_command_output_lines", 50)),
            max_test_output_lines=int(plan.get("max_test_output_lines", 50)),
            max_chars_per_file=max(int(plan.get("max_chars_per_file", 24000)), 16000),
        )

    def build(self) -> dict[str, Any]:
        repo_map = self._build_repo_map()
        search_terms = [str(term).strip() for term in self.plan.get("search_terms", []) if str(term).strip()]
        direct_files = self._direct_file_targets(repo_map)
        direct_symbol_terms = self._direct_symbol_targets()

        searches = [
            search_files(self.repo, term, self.limits.max_search_results)
            for term in search_terms
        ]
        symbol_searches = [
            search_files(self.repo, term, self.limits.max_search_results)
            for term in direct_symbol_terms
        ]

        selected_files = self._merge_selected_files(
            self._deterministic_file_hits(direct_files),
            self._select_files(searches + symbol_searches),
            repo_map.get("priority_files", []),
        )

        evidence = [
            read_line_range(
                self.repo,
                path,
                1,
                self.limits.max_lines_per_file,
                self.limits.max_chars_per_file,
            )
            for path in selected_files
        ]

        tests = None
        if self.plan.get("test_command"):
            tests = run_test_command(
                self.repo,
                list(self.plan["test_command"]),
                self.limits.max_test_output_lines,
            )

        packet = {
            "task": self.plan.get("task", ""),
            "repository": self.repo.name,
            "repository_path": str(self.repo),
            "limits": self.limits.__dict__,
            "repo_map": repo_map,
            "git": git_summary(self.repo, self.limits.max_command_output_lines),
            "manifest_sample": rg_files(self.repo, self.limits.max_search_results),
            "deterministic_targets": {
                "files": direct_files,
                "symbols": direct_symbol_terms,
            },
            "searches": searches,
            "symbol_searches": symbol_searches,
            "evidence": evidence,
            "symbols": self._python_symbols(selected_files),
            "tests": tests,
            "summary": {
                "search_count": len(searches) + len(symbol_searches),
                "files_inspected": len(evidence),
                "lines_captured": sum(
                    max(0, item["line_end"] - item["line_start"] + 1)
                    for item in evidence
                    if item.get("content") != "[REDACTED_SECRET_FILE]"
                ),
                "tests_executed": 1 if tests else 0,
                "truncated": any(item.get("truncated") for item in evidence)
                or any(item.get("truncated") for item in searches)
                or any(item.get("truncated") for item in symbol_searches),
            },
        }
        return packet

    def _build_repo_map(self) -> dict[str, Any]:
        all_source_files = _python_files_fallback(self.repo, max_results=200)
        
        # Exclude pure __init__.py unless necessary
        non_init_files = [p for p in all_source_files if Path(p).name != "__init__.py"]
        
        # Standard entry points across tech stacks
        entry_point_names = {
            "main.py", "app.py", "cli.py", "server.py", "index.py", "manage.py", "__main__.py",
            "index.js", "index.ts", "main.js", "main.ts", "server.js", "server.ts", "app.js", "app.ts",
            "main.go", "main.rs", "main.cpp", "Main.java", "Program.cs"
        }
        entry_points = [p for p in non_init_files if Path(p).name.lower() in entry_point_names]
        
        # Config files across tech stacks
        config_exts = {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg"}
        configs = [
            p for p in non_init_files
            if ("config" in p.lower() or "setting" in p.lower() or Path(p).suffix.lower() in config_exts or Path(p).name.startswith(".env"))
            and not ("test" in p.lower() or "cache" in p.lower() or "lock" in p.lower())
        ]
        
        # Dependency manifests
        manifest_names = {
            "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
            "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Gemfile", "CMakeLists.txt"
        }
        manifests = [p for p in all_source_files if Path(p).name in manifest_names or Path(p).name.startswith("requirements")]
        
        # Test files: strict directory matching and test_ prefix / _test suffix
        tests = [
            p for p in non_init_files
            if self._is_test_file(p)
        ]
        
        # Core source files grouped by top-level directory
        core_files = [
            p for p in non_init_files
            if p not in entry_points and p not in configs and p not in manifests and p not in tests
        ]
        
        # Top-level packages/directories
        top_dirs: dict[str, list[str]] = {}
        for p in core_files:
            parts = Path(p).parts
            top_dir = parts[0] if len(parts) > 1 else "root"
            top_dirs.setdefault(top_dir, []).append(p)
        
        priority_files: list[str] = []
        for ep in entry_points:
            if ep not in priority_files:
                priority_files.append(ep)
        
        # Distribute selection across top directories
        for top_dir, dir_files in top_dirs.items():
            for f in dir_files[:3]:
                if f not in priority_files:
                    priority_files.append(f)
                    
        for cfg in configs:
            if cfg not in priority_files:
                priority_files.append(cfg)
                
        for man in manifests:
            if man not in priority_files:
                priority_files.append(man)
                
        for t in tests[:3]:
            if t not in priority_files:
                priority_files.append(t)
                
        for f in core_files:
            if f not in priority_files:
                priority_files.append(f)

        return {
            "total_discovered_files": len(all_source_files),
            "entry_points": entry_points,
            "configs": configs,
            "manifests": manifests,
            "top_packages": list(top_dirs.keys()),
            "tests": tests,
            "priority_files": priority_files,
        }

    def _is_test_file(self, rel_path: str) -> bool:
        p = Path(rel_path)
        name = p.name.lower()
        parts = [part.lower() for part in p.parts[:-1]]
        if any(part in {"tests", "test", "spec", "__tests__", "testing"} for part in parts):
            return True
        if name.startswith("test_") or name.endswith("_test.py") or name.endswith(".test.js") or name.endswith(".test.ts") or name.endswith(".spec.js") or name.endswith(".spec.ts"):
            return True
        return False

    def _direct_file_targets(self, repo_map: dict[str, Any] | None = None) -> list[str]:
        corpus = self._task_corpus()
        targets: list[str] = []
        for match in re.finditer(r"(?<![\w./-])([A-Za-z0-9_.-]+\.[A-Za-z0-9]+)(?![\w./-])", corpus):
            candidate = match.group(1)
            if candidate not in targets and self._is_repo_path(candidate):
                targets.append(candidate)

        if not targets and repo_map:
            targets.extend(repo_map.get("priority_files", [])[: self.limits.max_files])

        return targets[: self.limits.max_files]

    def _direct_symbol_targets(self) -> list[str]:
        corpus = self._task_corpus()
        symbols: list[str] = []
        for match in re.finditer(r"(?<![\w])([A-Za-z_][A-Za-z0-9_]{1,50})(?![\w])", corpus):
            token = match.group(1)
            lowered = token.lower()
            if lowered in {"goal", "openhands", "builder", "review", "pipeline", "audit", "perform", "technical", "comprehensive"}:
                continue
            if token not in symbols:
                symbols.append(token)
        return symbols[:8]

    def _deterministic_file_hits(self, paths: list[str]) -> list[str]:
        seen: set[str] = set()
        selected: list[str] = []
        for path in paths:
            if path in seen:
                continue
            if self._is_repo_path(path) and (self.repo / path).exists():
                seen.add(path)
                selected.append(path)
        return selected

    def _merge_selected_files(self, direct: list[str], searched: list[str], priority: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for path in [*direct, *searched, *priority]:
            if path in seen:
                continue
            if (self.repo / path).exists() and not (self.repo / path).is_dir():
                seen.add(path)
                merged.append(path)
            if len(merged) >= self.limits.max_files:
                break
        return merged

    def _task_corpus(self) -> str:
        return " ".join(
            [
                str(self.plan.get("task", "")),
                " ".join(str(item) for item in self.plan.get("search_terms", []) if str(item).strip()),
            ]
        )

    def _is_repo_path(self, candidate: str) -> bool:
        if "/" in candidate or "\\" in candidate:
            return True
        return Path(candidate).suffix.lower() in {".py", ".json", ".md", ".txt", ".js", ".ts", ".go", ".rs", ".yaml", ".yml", ".toml"}

    def _select_files(self, searches: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        selected: list[str] = []
        for result in searches:
            for path in result.get("files", []):
                if path not in seen:
                    seen.add(path)
                    selected.append(path)
                if len(selected) >= self.limits.max_files:
                    return selected
        return selected

    def _python_symbols(self, selected_files: list[str]) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        for relative in selected_files:
            if not relative.endswith(".py"):
                continue
            target_path = self.repo / relative
            if not target_path.exists() or target_path.is_dir():
                continue
            try:
                tree = ast.parse(target_path.read_text(encoding="utf-8", errors="replace"))
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbols.append({"path": relative, "name": node.name, "line": node.lineno})
        return symbols[:100]


def build_evidence(repo: str | Path, plan_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    packet = EvidenceBuilder(repo, plan).build()
    Path(output_path).write_text(json.dumps(packet, indent=2, ensure_ascii=False), encoding="utf-8")
    return packet
