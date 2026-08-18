from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from evidence.builder import EvidenceBuilder
from evidence.search import rg_files, search_files


class EvidenceBuilderTests(unittest.TestCase):
    def test_explicit_file_and_symbol_harvest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            plan = {"task": "Edit calculator.py and keep add behavior stable.", "search_terms": ["calculator.py", "add"], "max_files": 5, "max_lines_per_file": 50}
            packet = EvidenceBuilder(repo, plan).build()
            paths = {item["path"] for item in packet["evidence"]}
            symbols = {(item["path"], item["name"]) for item in packet["symbols"]}
            self.assertIn("calculator.py", paths)
            self.assertIn(("calculator.py", "add"), symbols)
            self.assertGreaterEqual(packet["summary"]["search_count"], 2)

    def test_duplicate_results_are_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            plan = {"task": "calculator.py add add add", "search_terms": ["calculator.py", "calculator.py", "add"], "max_files": 5, "max_lines_per_file": 50}
            packet = EvidenceBuilder(repo, plan).build()
            paths = [item["path"] for item in packet["evidence"]]
            self.assertEqual(paths.count("calculator.py"), 1)

    def test_nonexistent_explicit_file_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            plan = {"task": "Inspect missing_file.py and add", "search_terms": ["missing_file.py"], "max_files": 5, "max_lines_per_file": 50}
            packet = EvidenceBuilder(repo, plan).build()
            self.assertTrue(packet["evidence"])
            self.assertNotIn("missing_file.py", [item["path"] for item in packet["evidence"]])

    def test_secret_file_remains_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            self._git(repo, "add", ".env")
            self._git(repo, "commit", "-m", "add secret file")
            plan = {"task": "calculator.py .env token", "search_terms": ["calculator.py", ".env", "token"], "max_files": 5, "max_lines_per_file": 50}
            packet = EvidenceBuilder(repo, plan).build()
            self.assertNotIn(".env", [item["path"] for item in packet["evidence"]])

    def test_rg_permission_error_uses_git_file_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            real_run = subprocess.run

            def run_with_blocked_rg(args, *pargs, **kwargs):
                if isinstance(args, (list, tuple)) and args and str(args[0]).lower() == "rg":
                    raise PermissionError("rg blocked by Windows policy")
                return real_run(args, *pargs, **kwargs)

            with patch("evidence.search.subprocess.run", side_effect=run_with_blocked_rg):
                files = rg_files(repo, 10)
                result = search_files(repo, "return a + b", 10)

            self.assertIn("calculator.py", files)
            self.assertIn("calculator.py", result["files"])

    def test_git_search_fallback_does_not_truncate_candidates_before_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp) / "repo")
            for index in range(80):
                (repo / f"aaa_{index:03d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
            (repo / "zz_target.py").write_text("UNIQUE_TARGET_MARKER = True\n", encoding="utf-8")
            self._git(repo, "add", ".")
            self._git(repo, "commit", "-m", "large fallback fixture")
            real_run = subprocess.run

            def run_with_blocked_rg(args, *pargs, **kwargs):
                if isinstance(args, (list, tuple)) and args and str(args[0]).lower() == "rg":
                    raise PermissionError("rg blocked by Windows policy")
                return real_run(args, *pargs, **kwargs)

            with patch("evidence.search.subprocess.run", side_effect=run_with_blocked_rg):
                result = search_files(repo, "UNIQUE_TARGET_MARKER", 5)

            self.assertEqual(result["files"], ["zz_target.py"])
            self.assertFalse(result["truncated"])

    def _init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init")
        self._git(path, "config", "user.email", "test@example.com")
        self._git(path, "config", "user.name", "Test User")
        (path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (path / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
        self._git(path, "add", ".")
        self._git(path, "commit", "-m", "init")
        return path

    def _git(self, path: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
