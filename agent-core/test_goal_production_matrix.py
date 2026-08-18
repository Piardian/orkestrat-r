from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest

from evidence.builder import EvidenceBuilder
from goal import GoalMetricsService, GoalService, GoalStatusService
from goal.finalize import FinalReviewService
from goal.resume_service import GoalResumeService


class GoalProductionHardeningMatrixTests(unittest.TestCase):
    # =========================================================================
    # FAZ A: CONCURRENCY & REPO-LEVEL APPLY MUTEX
    # =========================================================================
    def test_concurrent_parallel_goals_apply_serialized_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_fixture_git_repo(root / "repo")
            service = GoalService(base_dir=root / "runtime" / "goals")
            final_svc = FinalReviewService(service=service, runtime_root=root / "runtime")

            # Seed 3 independent goals with patches modifying different functions in app.py
            goals: list[str] = []
            for i in range(1, 4):
                goal_rec = service.create_goal(f"Concurrent goal {i}", repo)
                p1 = service.update_status(goal_rec, "PLANNING")
                p2 = service.update_status(p1, "PLANNED")
                p3 = service.update_status(p2, "REVIEWING")
                p4 = service.update_status(p3, "APPROVED")
                p5 = service.update_status(p4, "COMPLEXITY_ASSESSING")
                p6 = service.update_status(p5, "READY_FOR_OPENHANDS")
                p7 = service.update_status(p6, "BUILDING")
                built = service.update_status(p7, "BUILT_PENDING_REVIEW")

                # Worktree edit
                ws = root / "runtime" / "workspaces" / built.goal_id
                ws.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "--force", str(ws), "HEAD"], check=True, capture_output=True)
                (ws / f"module_{i}.py").write_text(f"def func_{i}(): return {i}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(ws), "add", "-N", f"module_{i}.py"], check=True, capture_output=True)
                patch = subprocess.run(["git", "-C", str(ws), "diff", "--binary", "--", f"module_{i}.py"], capture_output=True, text=True, check=True).stdout

                service.store.save_text(built.goal_id, "build.patch", patch)
                service.store.save_plan(built.goal_id, "build_request.json", {
                    "goal_id": built.goal_id,
                    "target_repo": str(repo),
                    "allowed_files": [f"module_{i}.py"],
                    "verification_commands": [f"{sys.executable} -c \"pass\""],
                })
                service.store.save_plan(built.goal_id, "build.json", {
                    "goal_id": built.goal_id,
                    "status": "BUILT_PENDING_REVIEW",
                    "changed_files": [f"module_{i}.py"],
                })
                reviewed, _ = final_svc.review(built.goal_id)
                self.assertEqual(reviewed.status, "READY_TO_APPLY")
                goals.append(built.goal_id)

            # Apply the 3 goals concurrently using ThreadPoolExecutor
            def _apply_goal(gid: str):
                return final_svc.apply(gid, explicit_apply=True)

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(_apply_goal, goals))

            # At least the first one succeeds cleanly, and any subsequent ones with base commit change
            # fail-fast cleanly with BASE_CHANGED or complete safely through the repo lock
            completed_count = sum(1 for rec, res in results if rec.status == "COMPLETED")
            blocked_count = sum(1 for rec, res in results if rec.status == "APPLY_FAILED")
            self.assertEqual(completed_count + blocked_count, 3)
            self.assertGreaterEqual(completed_count, 1)

    # =========================================================================
    # FAZ B: REWARD HACKING & DUAL VERIFICATION (PUBLIC VS HIDDEN TESTS)
    # =========================================================================
    def test_reward_hacking_cheat_solution_rejected_by_hidden_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_fixture_git_repo(root / "repo")
            service = GoalService(base_dir=root / "runtime" / "goals")
            final_svc = FinalReviewService(service=service, runtime_root=root / "runtime")

            record = service.create_goal("Implement multiply function", repo)
            p1 = service.update_status(record, "PLANNING")
            p2 = service.update_status(p1, "PLANNED")
            p3 = service.update_status(p2, "REVIEWING")
            p4 = service.update_status(p3, "APPROVED")
            p5 = service.update_status(p4, "COMPLEXITY_ASSESSING")
            p6 = service.update_status(p5, "READY_FOR_OPENHANDS")
            p7 = service.update_status(p6, "BUILDING")
            built = service.update_status(p7, "BUILT_PENDING_REVIEW")

            # The agent cheated: it hardcoded `return 6` because public test checks `multiply(2, 3) == 6`
            ws = root / "runtime" / "workspaces" / built.goal_id
            ws.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "--force", str(ws), "HEAD"], check=True, capture_output=True)
            (ws / "calc.py").write_text("def multiply(a, b):\n    return 6  # CHEAT / REWARD HACK\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(ws), "add", "-N", "calc.py"], check=True, capture_output=True)
            patch = subprocess.run(["git", "-C", str(ws), "diff", "--binary", "--", "calc.py"], capture_output=True, text=True, check=True).stdout

            public_verify = f"{sys.executable} -c \"import calc; assert calc.multiply(2, 3) == 6\""
            # Hidden test checks generalized properties with randomized inputs
            hidden_verify = f"{sys.executable} -c \"import calc; assert calc.multiply(4, 5) == 20 and calc.multiply(7, 8) == 56\""

            service.store.save_text(built.goal_id, "build.patch", patch)
            service.store.save_plan(built.goal_id, "build_request.json", {
                "goal_id": built.goal_id,
                "target_repo": str(repo),
                "allowed_files": ["calc.py"],
                "verification_commands": [public_verify],
                "private_verification_commands": [hidden_verify],
            })
            service.store.save_plan(built.goal_id, "build.json", {
                "goal_id": built.goal_id,
                "status": "BUILT_PENDING_REVIEW",
                "changed_files": ["calc.py"],
            })

            # Review MUST reject the cheated solution because private verification fails
            reviewed, summary = final_svc.review(built.goal_id)
            self.assertEqual(reviewed.status, "BUILD_REJECTED")
            self.assertFalse(summary.ready_to_apply)
            self.assertFalse(summary.verification_pass)

    # =========================================================================
    # FAZ C: LARGE REPOSITORY SCALE BENCHMARK (1,000+ FILES)
    # =========================================================================
    def test_large_repository_evidence_harvest_within_time_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "large_repo"
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True, capture_output=True)

            # Generate 1,000 synthetic files in nested subpackages
            for pkg_idx in range(20):
                pkg_dir = repo / f"pkg_{pkg_idx}"
                pkg_dir.mkdir(parents=True, exist_ok=True)
                for file_idx in range(50):
                    (pkg_dir / f"service_{file_idx}.py").write_text(
                        f"# service {pkg_idx}_{file_idx}\ndef process_{pkg_idx}_{file_idx}(x):\n    return x * 2\n",
                        encoding="utf-8",
                    )
            # Add target file
            (repo / "target_service.py").write_text("def target_function(data):\n    return data.strip()\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "add 1000 files"], check=True, capture_output=True)

            plan = {
                "task": "Modify target_function in target_service.py",
                "search_terms": ["target_function", "target_service.py"],
                "max_files": 5,
                "max_lines_per_file": 50,
            }

            t0 = time.time()
            packet = EvidenceBuilder(repo, plan).build()
            duration = time.time() - t0

            # Evidence collection across 1,000 files must finish in under 3.0 seconds
            self.assertLess(duration, 3.0, f"Evidence harvest took {duration:.2f}s, expected < 3.0s")
            paths = [item["path"] for item in packet["evidence"]]
            self.assertIn("target_service.py", paths)
            self.assertGreaterEqual(packet["summary"]["files_inspected"], 1)

    # =========================================================================
    # FAZ D: LONG-RUN RESILIENCE & IDEMPOTENT RESUME
    # =========================================================================
    def test_pipeline_multi_stage_crash_recovery_resumes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_fixture_git_repo(root / "repo")
            service = GoalService(base_dir=root / "runtime" / "goals")
            resume_svc = GoalResumeService(service=service, runtime_root=root / "runtime")

            # 1. Create and crash at PLANNING
            rec = service.create_goal("Resilient test", repo)
            service.update_status(rec, "PLANNING", phase="planning", note="simulated power loss")

            # Resume recovers from PLANNING to PLANNED
            res = resume_svc.resume(rec.goal_id, execute=True)
            self.assertTrue(res.executed)
            self.assertEqual(res.state, "PLANNED")

            # 2. Crash at REVIEWING
            planned_rec = service.read_goal(rec.goal_id)
            service.update_status(planned_rec, "REVIEWING", phase="reviewing", note="simulated network drop")

            # Resume recovers from REVIEWING to a valid review outcome (APPROVED / REVIEW_UNKNOWN / REVISION_REQUIRED)
            res2 = resume_svc.resume(rec.goal_id, execute=True)
            self.assertTrue(res2.executed)
            self.assertIn(res2.state, {"APPROVED", "REVIEW_UNKNOWN", "REVISION_REQUIRED"})

            # If not approved, approve it so complexity stage can be tested
            if res2.state != "APPROVED":
                service.update_status(service.read_goal(rec.goal_id), "APPROVED", phase="approved", note="force approve for test")

            # 3. Crash at COMPLEXITY_ASSESSING
            approved_rec = service.read_goal(rec.goal_id)
            service.update_status(approved_rec, "COMPLEXITY_ASSESSING", phase="complexity-assessing", note="simulated timeout")

            # Resume recovers from COMPLEXITY_ASSESSING to READY_FOR_OPENHANDS / CODEX
            res3 = resume_svc.resume(rec.goal_id, execute=True)
            self.assertTrue(res3.executed)
            self.assertIn(res3.state, {"READY_FOR_OPENHANDS", "CODEX_REQUIRED"})

    def _init_fixture_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True, capture_output=True)
        (path / ".gitignore").write_text("__pycache__/\n*.pyc\nruntime/\n", encoding="utf-8")
        (path / "app.py").write_text("def base_app(): return 'base'\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
        return path


if __name__ == "__main__":
    unittest.main()
