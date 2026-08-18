import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from goal import GoalService, GoalStore
from goal.finalize import FinalReviewService


class Phase7FinalGateTests(unittest.TestCase):
    def test_review_then_explicit_apply_completes_without_touching_original_repo_during_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            original = (repo / "app.py").read_text(encoding="utf-8")
            service, approved = self._seed_built_goal(root, repo)
            final = FinalReviewService(service=service, runtime_root=root / "runtime")

            # In the unpatched target repo, the verification command MUST fail
            unpatched_verify = subprocess.run(
                [sys.executable, "-c", "import app; assert getattr(app, 'VERSION', None) == 2, 'app.VERSION is not 2'"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(unpatched_verify.returncode, 0, "Unpatched code must fail verification")

            record, summary = final.review(approved.goal_id)
            self.assertEqual(record.status, "READY_TO_APPLY")
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "apply_manifest.json").exists())
            self.assertEqual(original, (repo / "app.py").read_text(encoding="utf-8"), "Target repo must not change during review")
            self.assertTrue(summary.ready_to_apply)
            self.assertTrue(summary.verification_pass)

            applied_record, result = final.apply(approved.goal_id, explicit_apply=True)
            self.assertEqual(applied_record.status, "COMPLETED")
            self.assertEqual(result["verification"]["status"], "PASS")
            self.assertNotEqual(original, (repo / "app.py").read_text(encoding="utf-8"))
            self.assertIn("VERSION = 2", (repo / "app.py").read_text(encoding="utf-8"))

    def test_apply_requires_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_built_goal(root, repo)
            final = FinalReviewService(service=service, runtime_root=root / "runtime")
            final.review(approved.goal_id)
            with self.assertRaises(ValueError):
                final.apply(approved.goal_id, explicit_apply=False)

    def test_review_rejects_corrupt_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_built_goal(root, repo, corrupt_patch=True)
            final = FinalReviewService(service=service, runtime_root=root / "runtime")
            record, summary = final.review(approved.goal_id)
            self.assertEqual(record.status, "BUILD_REJECTED")
            self.assertFalse(summary.ready_to_apply)

    def _seed_built_goal(self, root: Path, repo: Path, corrupt_patch: bool = False):
        store = GoalStore(root / "runtime" / "goals")
        service = GoalService(store)
        created = service.create_goal("Update app version", repo)
        planning = service.update_status(created, "PLANNING", phase="planning")
        planned = service.update_status(planning, "PLANNED", phase="planned")
        reviewing = service.update_status(planned, "REVIEWING", phase="reviewing")
        approved = service.update_status(reviewing, "APPROVED", phase="approved")
        assessing = service.update_status(approved, "COMPLEXITY_ASSESSING", phase="complexity-assessing")
        ready = service.update_status(assessing, "READY_FOR_OPENHANDS", phase="complexity-assessed")
        building = service.update_status(ready, "BUILDING", phase="building")
        built = service.update_status(building, "BUILT_PENDING_REVIEW", phase="built-pending-review")

        goal_dir = store.goal_dir(built.goal_id)
        verify_cmd = f"{sys.executable} -c \"import app; assert getattr(app, 'VERSION', None) == 2, 'app.VERSION is not 2'\""
        request = {
            "goal_id": built.goal_id,
            "goal": built.goal,
            "plan": {"candidate_files": ["app.py"], "acceptance_criteria": ["VERSION == 2"], "verification": [verify_cmd], "constraints": []},
            "review": {"final_verdict": "PASS"},
            "complexity": {"severity": "HARD", "recommended_executor": "codex"},
            "evidence": {"summary": {"files_inspected": 1}, "evidence": []},
            "allowed_files": ["app.py"],
            "forbidden_files": [".env"],
            "forbidden_areas": ["private keys"],
            "acceptance_criteria": ["VERSION == 2"],
            "verification_commands": [verify_cmd],
            "constraints": ["No secrets"],
            "complexity_reasons": ["patch risk"],
            "severity": "HARD",
            "recommended_executor": "codex",
            "source_repo": str(repo),
            "target_repo": str(repo),
            "runtime_workspace": str(root / "runtime" / "workspaces" / built.goal_id),
            "prompt_path": str(goal_dir / "codex_prompt.md"),
            "handoff_path": str(goal_dir / "codex_handoff.md"),
        }
        build = {
            "goal_id": built.goal_id,
            "executor": "codex-manual",
            "source": "manual-response",
            "changed_files": ["app.py"],
            "verification": "PASS",
            "status": "BUILT_PENDING_REVIEW",
            "failure_type": None,
            "patch_path": str(goal_dir / "build.patch"),
            "patch_size": 100,
            "verification_result": {"status": "PASS", "exit_code": 0},
            "original_repo_modified": False,
            "codex_response_path": str(goal_dir / "codex_response_001.txt"),
        }
        if corrupt_patch:
            patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-non_existent_line_content
+VERSION = 2
"""
        else:
            patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VERSION = 1
+VERSION = 2
"""
        store.save_plan(built.goal_id, "build_request.json", request)
        store.save_plan(built.goal_id, "build.json", build)
        store.save_text(built.goal_id, "build.patch", patch)
        store.save_plan(built.goal_id, "verification.json", {"status": "PASS", "command_results": [{"command": verify_cmd, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 0}]})
        return service, built

    def _init_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init")
        self._git(path, "config", "user.email", "test@example.com")
        self._git(path, "config", "user.name", "Test User")
        (path / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
        self._git(path, "add", ".")
        self._git(path, "commit", "-m", "init")
        return path

    def _git(self, path: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
