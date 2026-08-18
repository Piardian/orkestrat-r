from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import unittest

from goal import GoalCodexService, GoalPlan, GoalReview, GoalService, GoalStore
from goal.complexity import ComplexityAssessment, ComplexityFactor
from schemas import Review, SearchPlan, Verdict


class Phase6CodexTests(unittest.TestCase):
    def test_prepare_and_submit_codex_patch_without_touching_original_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            original = (repo / "app.py").read_text(encoding="utf-8")
            service, approved = self._seed_codex_required_goal(root, repo, candidate_files=["app.py"])
            codex = GoalCodexService(service=service, runtime_root=root / "runtime")

            record, request, prompt = codex.prepare(approved.goal_id)
            self.assertEqual(record.status, "WAITING_CODEX")
            self.assertIn("Codex Manual Patch Request", prompt)
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "codex_prompt.md").exists())
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "codex_request.json").exists())
            self.assertEqual(request.allowed_files, ["app.py"])

            response = """Implementation complete.
```diff
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print('before')
+print('after')
```
::END_CODEX::
"""
            final_record, artifact = codex.submit(approved.goal_id, response)
            self.assertEqual(final_record.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(artifact.status, "BUILT_PENDING_REVIEW")
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "verification.json").exists())
            self.assertEqual(original, (repo / "app.py").read_text(encoding="utf-8"))

    def test_submit_rejects_missing_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_codex_required_goal(root, repo, candidate_files=["app.py"])
            codex = GoalCodexService(service=service, runtime_root=root / "runtime")
            codex.prepare(approved.goal_id)

            final_record, artifact = codex.submit(approved.goal_id, "No patch here.")
            self.assertEqual(final_record.status, "CODEX_RESPONSE_INVALID")
            self.assertEqual(artifact.status, "CODEX_RESPONSE_INVALID")
            self.assertEqual(artifact.failure_type, "NO_PATCH_FOUND")

    def test_submit_rejects_unauthorized_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_codex_required_goal(root, repo, candidate_files=["app.py"])
            codex = GoalCodexService(service=service, runtime_root=root / "runtime")
            codex.prepare(approved.goal_id)

            response = """```diff
diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -0,0 +1 @@
+VALUE=1
```"""
            final_record, artifact = codex.submit(approved.goal_id, response)
            self.assertEqual(final_record.status, "CODEX_POLICY_VIOLATION")
            self.assertEqual(artifact.status, "CODEX_POLICY_VIOLATION")
            self.assertEqual(artifact.failure_type, "UNAUTHORIZED_FILE")

    def test_prepare_blocks_after_manual_attempt_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_codex_required_goal(root, repo, candidate_files=["app.py"])
            goal_dir = service.store.goal_dir(approved.goal_id)
            for index in range(1, 4):
                service.store.save_text(approved.goal_id, f"codex_response_{index:03d}.txt", "attempt")
            codex = GoalCodexService(service=service, runtime_root=root / "runtime")
            with self.assertRaises(ValueError):
                codex.prepare(approved.goal_id)

    def _seed_codex_required_goal(
        self,
        root: Path,
        repo: Path,
        candidate_files: list[str],
    ):
        store = GoalStore(root / "runtime" / "goals")
        service = GoalService(store)
        created = service.create_goal("Adjust app output", repo)
        planning = service.update_status(created, "PLANNING", phase="planning")
        planned = service.update_status(planning, "PLANNED", phase="planned")
        reviewing = service.update_status(planned, "REVIEWING", phase="reviewing")
        approved = service.update_status(reviewing, "APPROVED", phase="approved")
        search_plan = SearchPlan(task=approved.goal, search_terms=["app"])
        evidence = {"task": approved.goal, "repository": str(repo), "summary": {"files_inspected": 1}, "evidence": []}
        plan = GoalPlan(
            plan_version=1,
            goal_id=approved.goal_id,
            objective=approved.goal,
            summary="small change",
            tasks=[{"id": "TASK-1", "title": "Patch app.py", "description": "Update output", "depends_on": []}],
            candidate_files=candidate_files,
            acceptance_criteria=["app.py changes"],
            verification=["py -3 -c \"print('ok')\""],
            risks=[],
            constraints=["No secrets"],
            patch_expected=True,
            uncertainties=[],
            evidence_refs=[],
        )
        review = Review(
            final_verdict="PASS",
            confidence=0.95,
            agreement="FULL",
            reason="Approved for Codex handoff.",
            analyst_a={"verdict": "PASS"},
            analyst_b={"verdict": "PASS"},
            analysts=[{"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}],
            evidence=[],
            patch_required=True,
            risk_flags=[],
        )
        analyst_payload = [
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[], analyst="analyst-1", profile="gemini-user-b", uncertainties=[], risk_flags=[]).to_dict(),
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[], analyst="analyst-2", profile="gemini-user-c", uncertainties=[], risk_flags=[]).to_dict(),
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[], analyst="analyst-3", profile="gemini-user-a", uncertainties=[], risk_flags=[]).to_dict(),
        ]
        complexity = ComplexityAssessment(
            version=1,
            goal_id=approved.goal_id,
            score=11,
            severity="HARD",
            recommended_executor="codex",
            factors=[ComplexityFactor(name="patch_risk", score=5, reason="Codex escalation is required")],
            hard_overrides=["patch_required"],
            candidate_file_count=len(candidate_files),
            module_count=1,
            review_risk_count=1,
            llm_calls=4,
        )
        store.save_plan_bundle(approved, search_plan.to_dict(), evidence, plan)
        store.save_review_bundle(
            approved,
            analyst_payload,
            GoalReview(
                goal_id=approved.goal_id,
                task=approved.goal,
                reviewer_profile="gemini-user-d",
                status="APPROVED",
                agreement=review.agreement,
                final_verdict=review.final_verdict,
                confidence=review.confidence,
                reason=review.reason,
                patch_required=review.patch_required,
                analyst_results=analyst_payload,
                reviewer_result=review.to_dict(),
                evidence_refs=[],
                provider_requests=4,
                logical_calls=4,
                provider_retries=0,
                json_repairs=0,
                stage_regenerations=0,
                input_tokens=40,
                output_tokens=32,
            ),
        )
        store.save_plan(approved.goal_id, "complexity.json", complexity.to_dict())
        approved_record = service.update_status(approved, "COMPLEXITY_ASSESSING", phase="complexity-assessing")
        approved_record = service.update_status(approved_record, "CODEX_REQUIRED", phase="complexity-assessed")
        return service, approved_record

    def _init_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init")
        self._git(path, "config", "user.email", "test@example.com")
        self._git(path, "config", "user.name", "Test User")
        (path / "app.py").write_text("print('before')\n", encoding="utf-8")
        self._git(path, "add", ".")
        self._git(path, "commit", "-m", "init")
        return path

    def _git(self, path: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
