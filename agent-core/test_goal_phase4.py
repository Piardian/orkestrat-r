from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from goal import GoalComplexityService, GoalService, GoalStore, GoalReview, GoalPlan
from goal.complexity import ComplexityAssessment
from goal.review import GoalReview
from schemas import Review, SearchPlan, Verdict


class Phase4GoalTests(unittest.TestCase):
    def test_easy_goal_routes_to_openhands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")

            service, planned = self._seed_approved_goal(root, repo)
            assessment_service = GoalComplexityService(service=service)
            record, assessment = assessment_service.assess_goal(planned.goal_id)

            self.assertEqual(record.status, "READY_FOR_OPENHANDS")
            self.assertEqual(assessment.severity, "EASY")
            self.assertEqual(assessment.recommended_executor, "openhands")
            self.assertEqual(assessment.llm_calls, 0)
            self.assertTrue((service.store.goal_dir(planned.goal_id) / "complexity.json").exists())

    def test_hard_security_goal_routes_to_openhands_in_mvp_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "auth").mkdir()
            (repo / "auth" / "migration.sql").write_text("alter table users add column secret text;\n", encoding="utf-8")

            service, planned = self._seed_approved_goal(
                root,
                repo,
                candidate_files=["auth/migration.sql", "auth/handler.py", "tests/auth_test.py", "docs/auth.md", "config/auth.yml"],
                summary="Security/auth refactor for public API and database migration.",
                tasks=[
                    {"id": "TASK-1", "title": "Refactor auth", "description": "Refactor authentication flows", "depends_on": []},
                    {"id": "TASK-2", "title": "DB migration", "description": "Apply destructive DB migration", "depends_on": ["TASK-1"]},
                ],
                verification=[],
                acceptance_criteria=[],
                risks=["security", "database migration"],
                uncertainties=["boundary behavior unclear"],
                review_risk_flags=["security risk"],
            )
            assessment_service = GoalComplexityService(service=service)
            with patch.dict("os.environ", {"AGENT_ARMY_OPENHANDS_ONLY": "true"}, clear=False):
                record, assessment = assessment_service.assess_goal(planned.goal_id)

            self.assertEqual(record.status, "READY_FOR_OPENHANDS")
            self.assertEqual(assessment.severity, "CRITICAL")
            self.assertEqual(assessment.recommended_executor, "openhands")
            self.assertGreaterEqual(assessment.score, 9)
            self.assertTrue(assessment.hard_overrides)

    def test_hard_goal_routes_to_codex_only_when_openhands_only_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            service, planned = self._seed_approved_goal(
                root,
                repo,
                candidate_files=["auth/migration.sql"],
                summary="Security database migration.",
                risks=["security", "database migration"],
            )

            with patch.dict("os.environ", {"AGENT_ARMY_OPENHANDS_ONLY": "false"}, clear=False):
                record, assessment = GoalComplexityService(service=service).assess_goal(planned.goal_id)

            self.assertEqual(record.status, "CODEX_REQUIRED")
            self.assertEqual(assessment.severity, "CRITICAL")
            self.assertEqual(assessment.recommended_executor, "codex")

    def test_non_approved_goal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(root / "runtime" / "goals"))
            created = service.create_goal("Build login", repo)
            assessment_service = GoalComplexityService(service=service)
            with self.assertRaises(ValueError):
                assessment_service.assess_goal(created.goal_id)

    def test_existing_complexity_artifact_blocks_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            service, planned = self._seed_approved_goal(root, repo)
            goal_dir = service.store.goal_dir(planned.goal_id)
            goal_dir.mkdir(parents=True, exist_ok=True)
            (goal_dir / "complexity.json").write_text("{}", encoding="utf-8")
            assessment_service = GoalComplexityService(service=service)
            with self.assertRaises(FileExistsError):
                assessment_service.assess_goal(planned.goal_id)

    def test_target_repo_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            repo_file = repo / "service.py"
            repo_file.write_text("print('before')\n", encoding="utf-8")
            before = repo_file.read_text(encoding="utf-8")

            service, planned = self._seed_approved_goal(root, repo)
            GoalComplexityService(service=service).assess_goal(planned.goal_id)

            after = repo_file.read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def _seed_approved_goal(
        self,
        root: Path,
        repo: Path,
        candidate_files: list[str] | None = None,
        summary: str = "Implement small change.",
        tasks: list[dict] | None = None,
        verification: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        risks: list[str] | None = None,
        uncertainties: list[str] | None = None,
        review_risk_flags: list[str] | None = None,
    ):
        candidate_files = candidate_files or ["app.py"]
        tasks = tasks or [{"id": "TASK-1", "title": "Small change", "description": "Adjust one function", "depends_on": []}]
        verification = verification if verification is not None else ["run tests"]
        acceptance_criteria = acceptance_criteria if acceptance_criteria is not None else ["works"]
        risks = risks or []
        uncertainties = uncertainties or []
        review_risk_flags = review_risk_flags or []

        store = GoalStore(root / "runtime" / "goals")
        service = GoalService(store)
        created = service.create_goal("Build login", repo)
        planning = service.update_status(created, "PLANNING", phase="planning")
        planned = service.update_status(planning, "PLANNED", phase="planned")
        reviewing = service.update_status(planned, "REVIEWING", phase="reviewing")
        approved = service.update_status(reviewing, "APPROVED", phase="approved")

        search_plan = SearchPlan(task=approved.goal, search_terms=["login"])
        evidence = {
            "task": approved.goal,
            "repository": str(repo),
            "summary": {"files_inspected": 1, "lines_captured": 1},
            "evidence": [{"path": "app.py", "line_start": 1, "line_end": 1}],
        }
        plan = GoalPlan(
            plan_version=1,
            goal_id=approved.goal_id,
            objective=approved.goal,
            summary=summary,
            tasks=tasks,
            candidate_files=candidate_files,
            acceptance_criteria=acceptance_criteria,
            verification=verification,
            risks=risks,
            constraints=["No secrets"],
            patch_expected=True,
            uncertainties=uncertainties,
            evidence_refs=["app.py:1-1"],
        )
        review = Review(
            final_verdict="PASS",
            confidence=0.95,
            agreement="FULL",
            reason="Looks good.",
            analyst_a={"verdict": "PASS"},
            analyst_b={"verdict": "PASS"},
            analysts=[{"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}],
            evidence=[{"path": "app.py", "lines": "1-1"}],
            patch_required=False,
            risk_flags=review_risk_flags,
        )
        analyst_payload = [
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[{"path": "app.py", "lines": "1-1"}], analyst="analyst-1", profile="gemini-user-b", uncertainties=[], risk_flags=[]).to_dict(),
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[{"path": "app.py", "lines": "1-1"}], analyst="analyst-2", profile="gemini-user-c", uncertainties=[], risk_flags=[]).to_dict(),
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[{"path": "app.py", "lines": "1-1"}], analyst="analyst-3", profile="gemini-user-a", uncertainties=uncertainties, risk_flags=review_risk_flags).to_dict(),
        ]

        store.save_plan_bundle(approved, search_plan.to_dict(), evidence, plan)
        store.save_review_bundle(approved, analyst_payload, GoalReview(
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
            evidence_refs=["app.py:1-1"],
            provider_requests=4,
            logical_calls=4,
            provider_retries=0,
            json_repairs=0,
            stage_regenerations=0,
            input_tokens=40,
            output_tokens=32,
        ))
        return service, approved


if __name__ == "__main__":
    unittest.main()
