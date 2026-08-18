from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from llm.client import BaseLLMClient, LLMResponse
from goal import GoalService, GoalStore
from goal.planner import GoalPlanner
from goal.model import GoalRecord


class FakeClient(BaseLLMClient):
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str, **options):
        self.calls += 1
        if self.calls == 1:
            text = json.dumps(
                {
                    "task": "search",
                    "search_terms": ["jwt", "login"],
                    "max_files": 3,
                    "max_lines_per_file": 40,
                }
            )
            return LLMResponse(text=text, input_tokens=12, output_tokens=9, retry_count=0, finish_reason="STOP")
        text = json.dumps(
            {
                "plan_version": 1,
                "goal_id": "GOAL-20260814-0001",
                "objective": "Build JWT login",
                "summary": "Implement JWT login flow with tests.",
                "tasks": [
                    {"id": "TASK-001", "title": "Add auth route", "description": "Create auth route", "depends_on": []}
                ],
                "candidate_files": ["server/auth.ts", "tests/auth.test.ts"],
                "acceptance_criteria": ["Login works", "Tests pass"],
                "verification": ["run tests"],
                "risks": ["JWT expiry"],
                "constraints": ["No secret leak"],
                "patch_expected": True,
                "uncertainties": [],
                "evidence_refs": ["server/auth.ts:1-20"],
            }
        )
        return LLMResponse(text=text, input_tokens=34, output_tokens=22, retry_count=0, finish_reason="STOP")


class FakeRouter:
    def __init__(self, client: BaseLLMClient) -> None:
        self.client = client

    def get_client(self, profile_id: str):
        return self.client


class Phase2GoalTests(unittest.TestCase):
    def test_goal_planner_creates_planned_state_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "server").mkdir()
            (repo / "server" / "auth.ts").write_text("export const auth = true;\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "auth.test.ts").write_text("test('auth', () => {});\n", encoding="utf-8")

            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Build JWT login", repo)
            planner = GoalPlanner(service=service, router=FakeRouter(FakeClient()))
            planned, search_plan, evidence, plan, usage = planner.plan_goal(created.goal_id, "nemotron-main")

            self.assertEqual(planned.status, "PLANNED")
            self.assertEqual(planned.phase, "planned")
            self.assertEqual(usage["logical_calls"], 2)
            self.assertEqual(usage["provider_requests"], 2)
            self.assertEqual(search_plan.search_terms, ["jwt", "login"])
            self.assertEqual(plan.status, "PLANNED")
            self.assertTrue((store.goal_dir(created.goal_id) / "goal.json").exists())
            self.assertTrue((store.goal_dir(created.goal_id) / "search_plan.json").exists())
            self.assertTrue((store.goal_dir(created.goal_id) / "evidence.json").exists())
            self.assertTrue((store.goal_dir(created.goal_id) / "plan.json").exists())

    def test_invalid_transition_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(Path(tmp) / "runtime" / "goals"))
            created = service.create_goal("Build JWT login", repo)
            with self.assertRaises(ValueError):
                service.update_status(created, "PLANNED")

    def test_goal_id_uses_utc_standard(self) -> None:
        service = GoalService(GoalStore(Path(tempfile.gettempdir()) / "goal-test-store"))
        goal_id = service._next_goal_id()
        self.assertRegex(goal_id, r"^GOAL-\d{8}-\d{4}$")


if __name__ == "__main__":
    unittest.main()
