from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest

from goal import GoalMetricsService, GoalService, GoalStatusService
from goal.finalize import FinalReviewService
from goal.resume_service import GoalResumeService, _is_pid_alive


class GoalFaultInjectionTests(unittest.TestCase):
    def test_stale_lock_with_dead_pid_is_cleared_and_acquired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_fixture_repo(root / "repo")
            service = GoalService(base_dir=root / "runtime" / "goals")
            record = service.create_goal("Test stale lock recovery", repo)
            resume_svc = GoalResumeService(service=service, runtime_root=root / "runtime")

            # Write a fake dead PID to lock file
            dead_pid = 99999999
            self.assertFalse(_is_pid_alive(dead_pid))
            lock_path = root / "runtime" / "locks" / f"{record.goal_id}.lock"
            lock_path.write_text(f"{dead_pid} {int(time.time()) - 100}", encoding="utf-8")

            # Resume must detect dead PID, clear lock, and succeed
            res = resume_svc.resume(record.goal_id, execute=True)
            self.assertTrue(res.lock_acquired)
            self.assertTrue(res.executed)

    def test_stale_in_flight_building_resumes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_fixture_repo(root / "repo")
            service = GoalService(base_dir=root / "runtime" / "goals")
            record = service.create_goal("Test building crash recovery", repo)
            # Transition to BUILDING
            p1 = service.update_status(record, "PLANNING")
            p2 = service.update_status(p1, "PLANNED")
            p3 = service.update_status(p2, "REVIEWING")
            p4 = service.update_status(p3, "APPROVED")
            p5 = service.update_status(p4, "COMPLEXITY_ASSESSING")
            p6 = service.update_status(p5, "READY_FOR_OPENHANDS")
            crashed = service.update_status(p6, "BUILDING", phase="building", note="crashed during build")

            snapshot = GoalStatusService(service).snapshot(record.goal_id)
            self.assertEqual(snapshot.stage, "BUILD")
            self.assertIn("in-flight", snapshot.current)

            resume_svc = GoalResumeService(service=service, runtime_root=root / "runtime")
            dry_run = resume_svc.resume(record.goal_id, execute=False)
            self.assertFalse(dry_run.executed)
            self.assertIn("Dry-run only", dry_run.message)

    def test_corrupt_patch_is_rejected_before_modifying_target_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_fixture_repo(root / "repo")
            orig_content = (repo / "calculator.py").read_text(encoding="utf-8")
            service = GoalService(base_dir=root / "runtime" / "goals")
            record = service.create_goal("Test corrupt patch rejection", repo)
            p1 = service.update_status(record, "PLANNING")
            p2 = service.update_status(p1, "PLANNED")
            p3 = service.update_status(p2, "REVIEWING")
            p4 = service.update_status(p3, "APPROVED")
            p5 = service.update_status(p4, "COMPLEXITY_ASSESSING")
            p6 = service.update_status(p5, "READY_FOR_OPENHANDS")
            p7 = service.update_status(p6, "BUILDING")
            built = service.update_status(p7, "BUILT_PENDING_REVIEW")

            # Corrupt patch
            corrupt_patch = "diff --git a/calculator.py b/calculator.py\n--- a/calculator.py\n+++ b/calculator.py\n@@ -99,1 +99,1 @@\n-nonexistent line\n+bad\n"
            service.store.save_text(built.goal_id, "build.patch", corrupt_patch)
            service.store.save_plan(built.goal_id, "build_request.json", {
                "goal_id": built.goal_id,
                "target_repo": str(repo),
                "allowed_files": ["calculator.py"],
                "verification_commands": [f"{sys.executable} -c \"pass\""],
            })
            service.store.save_plan(built.goal_id, "build.json", {
                "goal_id": built.goal_id,
                "status": "BUILT_PENDING_REVIEW",
                "changed_files": ["calculator.py"],
            })

            final_svc = FinalReviewService(service=service, runtime_root=root / "runtime")
            reviewed, summary = final_svc.review(built.goal_id)
            self.assertEqual(reviewed.status, "BUILD_REJECTED")
            self.assertFalse(summary.ready_to_apply)
            # Target repo must be untouched
            self.assertEqual((repo / "calculator.py").read_text(encoding="utf-8"), orig_content)

    def test_dirty_target_repo_prevents_apply_and_records_apply_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_fixture_repo(root / "repo")
            service = GoalService(base_dir=root / "runtime" / "goals")
            record = service.create_goal("Test dirty target repo block", repo)
            p1 = service.update_status(record, "PLANNING")
            p2 = service.update_status(p1, "PLANNED")
            p3 = service.update_status(p2, "REVIEWING")
            p4 = service.update_status(p3, "APPROVED")
            p5 = service.update_status(p4, "COMPLEXITY_ASSESSING")
            p6 = service.update_status(p5, "READY_FOR_OPENHANDS")
            p7 = service.update_status(p6, "BUILDING")
            built = service.update_status(p7, "BUILT_PENDING_REVIEW")

            valid_patch = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
-def add(a, b):
+def add(a: int, b: int) -> int:
     return a + b
"""
            service.store.save_text(built.goal_id, "build.patch", valid_patch)
            service.store.save_plan(built.goal_id, "build_request.json", {
                "goal_id": built.goal_id,
                "target_repo": str(repo),
                "allowed_files": ["calculator.py"],
                "verification_commands": [f"{sys.executable} -c \"pass\""],
            })
            service.store.save_plan(built.goal_id, "build.json", {
                "goal_id": built.goal_id,
                "status": "BUILT_PENDING_REVIEW",
                "changed_files": ["calculator.py"],
            })

            final_svc = FinalReviewService(service=service, runtime_root=root / "runtime")
            reviewed, summary = final_svc.review(built.goal_id)
            self.assertEqual(reviewed.status, "READY_TO_APPLY")

            # Dirty the target repo before apply
            (repo / "dirty_untracked_file.py").write_text("dirty\n", encoding="utf-8")

            # Apply should fail-fast without crashing state transitions
            failed_record, res = final_svc.apply(built.goal_id, explicit_apply=True)
            self.assertEqual(failed_record.status, "APPLY_FAILED")
            self.assertEqual(res["status"], "FAIL")

    def _init_fixture_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True, capture_output=True)
        (path / ".gitignore").write_text("__pycache__/\n*.pyc\nruntime/\n", encoding="utf-8")
        (path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
        return path


if __name__ == "__main__":
    unittest.main()
