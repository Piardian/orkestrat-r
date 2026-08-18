from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from goal import GoalMetricsService, GoalService, GoalStatusService
from goal.builder import BuilderResult
from goal.finalize import FinalReviewService
from goal.openhands_adapter import OpenHandsBuilderAdapter


class GoalEndToEndAcceptanceTests(unittest.TestCase):
    def test_full_pipeline_created_to_completed_twice_consecutively(self) -> None:
        for run_index in (1, 2):
            with self.subTest(run_index=run_index):
                self._run_single_full_acceptance_cycle(run_index)

    def _run_single_full_acceptance_cycle(self, cycle_index: int) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_dir = root / "runtime" / "goals"
            workspaces_dir = root / "runtime" / "workspaces"
            repo_dir = self._init_fixture_git_repo(root / "repo")

            # In unpatched repo, verify that type annotation test FAILS
            unpatched_test = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import inspect, calculator; sig = inspect.signature(calculator.add); assert sig.return_annotation is int and sig.parameters['a'].annotation is int, 'missing type hints'",
                ],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(unpatched_test.returncode, 0, "Unpatched fixture must fail type check verification")

            # 1. INTAKE (CREATED)
            service = GoalService(base_dir=runtime_dir)
            goal_text = f"Add Python type hints to calculator.py add function for cycle {cycle_index}"
            record = service.create_goal(goal_text, repo_dir)
            self.assertEqual(record.status, "CREATED")
            goal_id = record.goal_id

            # 2. PLANNING (PLANNED)
            verify_cmd = f"{sys.executable} -c \"import inspect, calculator; sig = inspect.signature(calculator.add); assert sig.return_annotation is int and sig.parameters['a'].annotation is int, 'missing type hints'\""
            planning = service.update_status(record, "PLANNING", phase="search-planning", note="planner started")
            search_plan = {"task": goal_text, "search_terms": ["calculator.py", "add"], "max_files": 5, "max_lines_per_file": 80}
            evidence = {"summary": {"files_inspected": 1, "lines_captured": 10}, "evidence": [{"path": "calculator.py", "line_start": 1, "line_end": 5, "content": "def add(a, b):\n    return a + b\n"}]}
            plan_payload = {
                "plan_version": 1,
                "goal_id": goal_id,
                "objective": goal_text,
                "summary": "Add int type hints to add(a: int, b: int) -> int",
                "tasks": [{"title": "Type annotate add", "description": "Add int hints"}],
                "candidate_files": ["calculator.py"],
                "acceptance_criteria": ["def add(a: int, b: int) -> int"],
                "verification": [verify_cmd],
                "risks": [],
                "constraints": ["Keep behavior identical"],
                "patch_expected": True,
                "uncertainties": [],
                "evidence_refs": ["calculator.py:1-5"],
            }
            service.store.save_plan(goal_id, "search_plan.json", search_plan)
            service.store.save_plan(goal_id, "evidence.json", evidence)
            service.store.save_plan(goal_id, "plan.json", plan_payload)
            planned = service.update_status(planning, "PLANNED", phase="planned", note="planning completed")
            self.assertEqual(planned.status, "PLANNED")

            # 3. REVIEW (APPROVED)
            reviewing = service.update_status(planned, "REVIEWING", phase="reviewing", note="review started")
            review_payload = {
                "final_verdict": "PASS",
                "confidence": 0.95,
                "agreement": "FULL",
                "reason": "Plan satisfies requirements and bounded scope.",
                "analysts": [],
                "evidence": ["calculator.py:1-5"],
                "patch_required": False,
            }
            service.store.save_plan(goal_id, "review.json", review_payload)
            approved = service.update_status(reviewing, "APPROVED", phase="reviewed", note="review verdict=PASS")
            self.assertEqual(approved.status, "APPROVED")

            # 4. COMPLEXITY GATE (READY_FOR_OPENHANDS)
            assessing = service.update_status(approved, "COMPLEXITY_ASSESSING", phase="complexity-assessing", note="complexity gate started")
            complexity_payload = {
                "version": 1,
                "goal_id": goal_id,
                "score": 1,
                "severity": "EASY",
                "recommended_executor": "openhands",
                "factors": [],
                "hard_overrides": [],
                "candidate_file_count": 1,
                "module_count": 1,
                "review_risk_count": 0,
                "llm_calls": 0,
            }
            service.store.save_plan(goal_id, "complexity.json", complexity_payload)
            ready_for_builder = service.update_status(assessing, "READY_FOR_OPENHANDS", phase="complexity-assessed", note="severity=EASY;executor=openhands")
            self.assertEqual(ready_for_builder.status, "READY_FOR_OPENHANDS")

            # 5. BUILDER EXECUTION (BUILT_PENDING_REVIEW)
            building = service.update_status(ready_for_builder, "BUILDING", phase="building", note="builder started")
            workspace_path = workspaces_dir / goal_id
            workspace_path.parent.mkdir(parents=True, exist_ok=True)
            # Create builder git worktree
            subprocess.run(["git", "-C", str(repo_dir), "worktree", "add", "--detach", "--force", str(workspace_path), "HEAD"], check=True, capture_output=True)

            # Apply edit to calculator.py inside workspace
            (workspace_path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

            # Verification inside staging workspace
            staged_verify = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import inspect, calculator; sig = inspect.signature(calculator.add); assert sig.return_annotation is int and sig.parameters['a'].annotation is int, 'missing type hints'",
                ],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(staged_verify.returncode, 0, "Staging workspace verification must PASS")

            # Build patch
            diff_res = subprocess.run(["git", "-C", str(workspace_path), "diff", "--binary", "--", "calculator.py"], capture_output=True, text=True, check=True)
            patch_content = diff_res.stdout
            self.assertTrue(patch_content.strip(), "Patch must not be empty")

            build_request = {
                "goal_id": goal_id,
                "goal": {"goal_id": goal_id, "goal": goal_text},
                "plan": plan_payload,
                "review": review_payload,
                "complexity": complexity_payload,
                "evidence": evidence,
                "mode": "default",
                "builder_profile": "gemini-builder",
                "allowed_files": ["calculator.py"],
                "forbidden_patterns": [".env"],
                "forbidden_areas": ["secrets"],
                "acceptance_criteria": ["def add(a: int, b: int) -> int"],
                "verification_commands": [verify_cmd],
                "constraints": ["Keep behavior identical"],
                "workspace_path": str(workspace_path),
                "target_repo": str(repo_dir),
                "allow_new_files": False,
            }
            build_result = {
                "goal_id": goal_id,
                "status": "BUILT_PENDING_REVIEW",
                "failure_type": None,
                "recommended_executor": "openhands",
                "changed_files": ["calculator.py"],
                "unauthorized_files": [],
                "patch_path": str(service.store.goal_dir(goal_id) / "build.patch"),
                "patch_size": len(patch_content.encode("utf-8")),
                "verification_status": "PASS",
                "verification_commands": [verify_cmd],
                "verification_result": {"status": "PASS", "exit_code": 0},
                "openhands_executed": True,
                "terminal_tool_enabled": False,
                "original_repo_modified": False,
                "provider_requests": 1,
                "provider_retries": 0,
                "builder_rate_limit_waits": 0,
                "builder_rate_limit_wait_seconds": 0.0,
                "provider_429_count": 0,
                "provider_503_count": 0,
                "provider_timeout_count": 0,
                "quota_exhausted_count": 0,
                "retry_exhausted": False,
            }
            service.store.save_plan(goal_id, "build_request.json", build_request)
            service.store.save_plan(goal_id, "build.json", build_result)
            service.store.save_text(goal_id, "build.patch", patch_content)
            built = service.update_status(building, "BUILT_PENDING_REVIEW", phase="built-pending-review", note="builder completed")
            self.assertEqual(built.status, "BUILT_PENDING_REVIEW")

            # Verify target repo NOT modified yet
            orig_calculator = (repo_dir / "calculator.py").read_text(encoding="utf-8")
            self.assertNotIn("-> int", orig_calculator, "Target repo must remain unmodified before apply")

            # 6. FINAL REVIEW (READY_TO_APPLY)
            final_review_service = FinalReviewService(service=service, runtime_root=root / "runtime")
            reviewed_record, review_summary = final_review_service.review(goal_id)
            self.assertEqual(reviewed_record.status, "READY_TO_APPLY")
            self.assertTrue(review_summary.ready_to_apply)
            self.assertTrue(review_summary.verification_pass)
            self.assertEqual(review_summary.changed_files, ["calculator.py"])

            # Verify target repo STILL unmodified after review
            self.assertNotIn("-> int", (repo_dir / "calculator.py").read_text(encoding="utf-8"))

            # 7. EXPLICIT APPLY (COMPLETED)
            completed_record, apply_result = final_review_service.apply(goal_id, explicit_apply=True)
            self.assertEqual(completed_record.status, "COMPLETED")
            self.assertEqual(apply_result["status"], "PASS")

            # Verify target repo IS modified after apply
            modified_calculator = (repo_dir / "calculator.py").read_text(encoding="utf-8")
            self.assertIn("-> int", modified_calculator, "Target repo must contain type hint after apply")

            # Post-apply verification on target repo
            post_apply_test = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import inspect, calculator; sig = inspect.signature(calculator.add); assert sig.return_annotation is int and sig.parameters['a'].annotation is int, 'missing type hints'",
                ],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            self.assertEqual(post_apply_test.returncode, 0, "Post-apply verification must PASS on target repo")

            # Check metrics and status snapshots
            metrics = GoalMetricsService(service, runtime_root=root / "runtime").refresh_goal(goal_id)
            self.assertEqual(metrics.state, "COMPLETED")
            self.assertEqual(metrics.result.get("executor"), "openhands")
            self.assertGreaterEqual(metrics.llm.get("provider_requests", 0), 1)

            snapshot = GoalStatusService(service).snapshot(goal_id)
            self.assertEqual(snapshot.state, "COMPLETED")
            self.assertIn("COMPLETED", snapshot.completed)

    def _init_fixture_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True, capture_output=True)
        (path / ".gitignore").write_text("__pycache__/\n*.pyc\nruntime/\n", encoding="utf-8")
        (path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (path / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\n\nclass TestCalc(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "initial commit"], check=True, capture_output=True)
        return path


if __name__ == "__main__":
    unittest.main()
