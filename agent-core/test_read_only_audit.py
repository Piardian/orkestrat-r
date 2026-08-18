from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from evidence.builder import EvidenceBuilder
from evidence.search import rg_files, search_files
from goal import GoalService, GoalStore, GoalPlanner, GoalReviewService
from goal.audit_report import is_read_only_audit, generate_audit_report
from goal.model import GoalRecord
from goal.plan import GoalPlan
from goal.status_service import GoalStatusService
from llm.router import LLMRouter
from schemas import Verdict, Review


class ReadOnlyAuditTests(unittest.TestCase):
    def test_read_only_audit_detection(self) -> None:
        self.assertTrue(is_read_only_audit("Bu projeyi baştan sona incele ve hiçbir dosyayı değiştirmeden detaylı bir teknik audit raporu hazırla."))
        self.assertTrue(is_read_only_audit("Perform comprehensive read-only audit of repo."))
        self.assertTrue(is_read_only_audit("Explain architecture and find bugs, do not modify files."))
        self.assertFalse(is_read_only_audit("Add type hints to calculator.py"))
        self.assertFalse(is_read_only_audit("Implement multiply function"))

    def test_turkish_utf8_lossless_roundtrip(self) -> None:
        turkish_text = "Türkçe karakter testi: ğüşiöç ĞÜŞİÖÇ baştan sona değiştirme olmaksızın."
        with tempfile.TemporaryDirectory() as tmp:
            store = GoalStore(Path(tmp) / "goals")
            service = GoalService(store)
            repo = Path(tmp) / "repo"
            repo.mkdir()

            record = service.create_goal(turkish_text, repo)
            self.assertEqual(record.goal_type, "READ_ONLY_AUDIT")

            loaded = store.load(record.goal_id)
            self.assertEqual(loaded.goal, turkish_text)

            # Test text save and load
            store.save_text(record.goal_id, "audit_report.md", turkish_text)
            loaded_text = (store.goal_dir(record.goal_id) / "audit_report.md").read_text(encoding="utf-8")
            self.assertEqual(loaded_text, turkish_text)

    def test_pyc_and_noisy_folders_excluded_and_source_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            # Create source files
            (repo / "main.py").write_text("def run():\n    print('main running')\n", encoding="utf-8")
            engine = repo / "engine"
            engine.mkdir()
            (engine / "backtest.py").write_text("class Backtester:\n    pass\n", encoding="utf-8")

            # Create noisy / cache / output directories
            pycache = repo / "__pycache__"
            pycache.mkdir()
            (pycache / "main.cpython-312.pyc").write_bytes(b"\x00\x00\x00\x00bytecode_junk_main")

            chat_history = repo / "chat_history"
            chat_history.mkdir()
            (chat_history / "chats.json").write_text('{"chat": "noise"}', encoding="utf-8")

            output_dir = repo / "output_test_run"
            output_dir.mkdir()
            (output_dir / "results.csv").write_text("col1,col2\n1,2\n", encoding="utf-8")

            # Search files
            files = rg_files(repo, max_results=20)
            self.assertIn("main.py", files)
            self.assertIn("engine/backtest.py", files)
            self.assertNotIn("__pycache__/main.cpython-312.pyc", files)
            self.assertNotIn("chat_history/chats.json", files)
            self.assertNotIn("output_test_run/results.csv", files)

            # Search term
            search_res = search_files(repo, "main", max_results=20)
            self.assertIn("main.py", search_res["files"])
            self.assertNotIn("__pycache__/main.cpython-312.pyc", search_res["files"])

    def test_non_git_repo_evidence_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "non_git_repo"
            repo.mkdir()
            (repo / "main.py").write_text("import os\nprint('hello')\n", encoding="utf-8")
            (repo / "config.json").write_text('{"env": "test"}', encoding="utf-8")

            plan = {
                "task": "Audit non-git project",
                "search_terms": ["hello", "test"],
            }
            builder = EvidenceBuilder(repo, plan)
            packet = builder.build()

            self.assertEqual(packet["git"]["is_repository"], False)
            self.assertIn("main.py", [e["path"] for e in packet["evidence"]])
            self.assertTrue(len(packet["evidence"]) >= 1)

    def test_git_repo_zero_commits_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "uncommitted_git_repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "main.py").write_text("def app(): pass", encoding="utf-8")
            (repo / "settings.py").write_text("TIMEOUT = 30", encoding="utf-8")

            plan = {
                "task": "Audit uncommitted repo",
                "search_terms": ["TIMEOUT"],
            }
            builder = EvidenceBuilder(repo, plan)
            packet = builder.build()

            self.assertEqual(packet["git"]["is_repository"], True)
            paths = [e["path"] for e in packet["evidence"]]
            self.assertIn("main.py", paths)
            self.assertIn("settings.py", paths)

    def test_audit_report_generation_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")

            evidence_packet = {
                "repo_map": {
                    "entry_points": ["main.py"],
                    "configs": [],
                    "core_modules": [],
                    "tests": [],
                    "manifests": [],
                },
                "evidence": [{"path": "main.py", "line_start": 1, "line_end": 2, "content": "def main():\n    pass"}],
                "symbols": [{"path": "main.py", "name": "main", "line": 1}],
            }

            analysts = [
                Verdict(verdict="PASS", confidence=0.9, reason="Good architecture", evidence=[{"path": "main.py", "lines": "1-2"}], analyst="analyst-1", profile="gemini-b"),
                Verdict(verdict="PASS", confidence=0.85, reason="Bounded scope", evidence=[{"path": "main.py", "lines": "1-2"}], analyst="analyst-2", profile="gemini-c"),
            ]
            review = Review(
                final_verdict="PASS",
                confidence=0.88,
                agreement="FULL",
                reason="Consensus on architecture",
                patch_required=False,
                analysts=[a.to_dict() for a in analysts],
                analyst_a=analysts[0].to_dict(),
                analyst_b=analysts[1].to_dict(),
                evidence=[{"path": "main.py", "lines": "1-2"}],
            )

            report = generate_audit_report(
                goal_id="GOAL-TEST-0001",
                goal_text="Perform audit",
                repo_path=repo,
                plan_dict={"patch_expected": False},
                evidence_packet=evidence_packet,
                analyst_results=analysts,
                review_dict=review.to_dict(),
            )

            self.assertIn("# Technical Audit Report", report)
            self.assertIn("## 1. Project Purpose & Scope", report)
            self.assertIn("## 2. Architecture & Subsystem Mapping", report)
            self.assertIn("## 3. Main Data Flow", report)
            self.assertIn("## 4. Verified Source-Backed Findings", report)
            self.assertIn("## 8. Reliability & Security Assessment", report)
            self.assertIn("## 10. Overall Conclusion", report)

    def test_read_only_audit_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GoalStore(Path(tmp) / "goals")
            service = GoalService(store)
            repo = Path(tmp) / "repo"
            repo.mkdir()

            record = service.create_goal("Bu projeyi incele ve audit raporu hazırla.", repo)
            self.assertEqual(record.goal_type, "READ_ONLY_AUDIT")

            status_service = GoalStatusService(service)
            snap = status_service.snapshot(record.goal_id)
            self.assertEqual(snap.goal_type, "READ_ONLY_AUDIT")
            self.assertEqual(snap.completed, ["INTAKE"])
            self.assertIn("AUDITING / REVIEW", snap.remaining)

            # Move through valid lifecycle: CREATED -> PLANNING -> PLANNED -> REVIEWING -> COMPLETED
            rec_planning = service.update_status(record, "PLANNING")
            rec_planned = service.update_status(rec_planning, "PLANNED")
            rec_reviewing = service.update_status(rec_planned, "REVIEWING")
            rec_completed = service.update_status(rec_reviewing, "COMPLETED", phase="audit-completed", note="audit completed")

            snap_completed = status_service.snapshot(record.goal_id)
            self.assertEqual(snap_completed.state, "COMPLETED")
            self.assertEqual(snap_completed.remaining, [])
            self.assertIn("Read-only audit report generated", snap_completed.current)


if __name__ == "__main__":
    unittest.main()
