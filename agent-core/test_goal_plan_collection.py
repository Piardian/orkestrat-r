import unittest
from typing import Any
import subprocess

from goal.plan import normalize_string_list
from goal.planner import GoalPlanner

class GoalPlanCollectionTests(unittest.TestCase):
    def test_normalize_string_list_with_string(self):
        val = normalize_string_list("python test.py")
        self.assertEqual(val, ["python test.py"])

    def test_normalize_string_list_with_list(self):
        val = normalize_string_list(["a", "b"])
        self.assertEqual(val, ["a", "b"])

    def test_normalize_string_list_with_none(self):
        val = normalize_string_list(None)
        self.assertEqual(val, [])

    def test_normalize_string_list_with_mixed_list(self):
        val = normalize_string_list(["  a  ", "", "b", None])
        self.assertEqual(val, ["a", "b", "None"])

    def test_normalize_string_list_rejects_invalid_type(self):
        with self.assertRaises(ValueError):
            normalize_string_list({"a": 1})

    def test_planner_validation_normalizes_fields(self):
        raw = {
            "summary": "Fix plan collection",
            "verification": "python test.py",
            "constraints": "Only modify calculator.py",
            "candidate_files": ["calculator.py", "  "]
        }
        planner = GoalPlanner(service=None, router=None)
        # Assuming goal_id and objective args are mockable
        plan = planner._validate_plan(raw, "GOAL-123", "test objective")
        
        # Check normalized fields
        self.assertEqual(plan.verification, ["python test.py"])
        self.assertEqual(plan.constraints, ["Only modify calculator.py"])
        self.assertEqual(plan.candidate_files, ["calculator.py"])

    def test_acceptance_planner_string_verification_does_not_split_to_chars(self):
        """
        Simulate GOAL-20260816-0001 bug where LLM returns string verification.
        Ensure it reaches BuilderRequest as a single string command, not characters,
        avoiding WinError 2.
        """
        from goal.service import GoalService
        from goal.builder_service import GoalBuilderService
        import tempfile
        import json
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "db.json").write_text("{}")
            (tmp / "test_repo").mkdir()
            subprocess.run(["git", "init"], cwd=str(tmp / "test_repo"), check=True, capture_output=True)

            service = GoalService(base_dir=tmp)
            rec = service.create_goal("Simulate planner string output", str(tmp / "test_repo"))
            rec = service.update_status(rec, "PLANNING", phase="mock")
            rec = service.update_status(rec, "PLANNED", phase="mock")
            rec = service.update_status(rec, "REVIEWING", phase="mock")
            rec = service.update_status(rec, "APPROVED", phase="mock")
            rec = service.update_status(rec, "COMPLEXITY_ASSESSING", phase="mock")
            rec = service.update_status(rec, "READY_FOR_OPENHANDS", phase="mock")

            # Mock a plan with a string verification
            plan_data = {
                "plan_version": 1,
                "goal_id": rec.goal_id,
                "objective": "test objective",
                "summary": "Fixing things",
                "verification": "python run_tests.py",  # STRING instead of LIST
                "candidate_files": ["calculator.py"],
                "allowed_files": ["calculator.py"],
                "constraints": "Only calculator", # STRING instead of LIST
                "tasks": [{"id": 1}],
                "patch_expected": True
            }
            
            # The GoalStore writes this to plan.json
            service.store.save_plan(rec.goal_id, "goal.json", rec.to_dict())
            
            # Here we test GoalPlan.from_dict which mimics how builder_service loads the plan
            from goal.plan import GoalPlan
            loaded_plan = GoalPlan.from_dict(plan_data).to_dict()
            service.store.save_plan(rec.goal_id, "plan.json", loaded_plan)
            
            service.store.save_plan(rec.goal_id, "review.json", {})
            service.store.save_plan(rec.goal_id, "complexity.json", {})
            service.store.save_plan(rec.goal_id, "evidence.json", {})

            builder_svc = GoalBuilderService(service=service, runtime_root=tmp / "workspaces")
            
            # Should correctly parse single string to array of 1 element, not characters
            _, request = builder_svc.dry_run(rec.goal_id)

            # Assert verification commands is exactly ['python run_tests.py']
            # Bug GOAL-20260816-0001 would produce ['p', 'y', 't', 'h', 'o', 'n', ...]
            self.assertEqual(request.verification_commands, ["python run_tests.py"])
            self.assertEqual(request.constraints, ["Only calculator"])
            self.assertEqual(request.allowed_files, ["calculator.py"])

if __name__ == "__main__":
    import subprocess
    unittest.main()
