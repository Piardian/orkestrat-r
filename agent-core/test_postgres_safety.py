from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from goal.planner import GoalPlanner
from goal.service import GoalService
from goal.store import GoalConcurrencyError, PostgresGoalStore
from llm.client import BaseLLMClient, LLMResponse


DATABASE_URL = os.getenv("AGENT_ARMY_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()


class FakePlannerClient(BaseLLMClient):
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str, **options):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                text=json.dumps(
                    {
                        "task": "search",
                        "search_terms": ["VALUE", "app"],
                        "max_files": 3,
                        "max_lines_per_file": 40,
                    }
                ),
                input_tokens=10,
                output_tokens=8,
                retry_count=0,
                finish_reason="STOP",
            )
        return LLMResponse(
            text=json.dumps(
                {
                    "plan_version": 1,
                    "summary": "Update app.py and verify the requested behavior.",
                    "tasks": [
                        {
                            "id": "TASK-001",
                            "title": "Update app",
                            "description": "Modify app.py",
                            "depends_on": [],
                        }
                    ],
                    "candidate_files": ["app.py"],
                    "allowed_files": ["app.py"],
                    "acceptance_criteria": ["Requested behavior is implemented"],
                    "verification": ["python -m unittest -v"],
                    "risks": [],
                    "constraints": ["Only edit app.py"],
                    "patch_expected": True,
                    "uncertainties": [],
                    "evidence_refs": ["app.py:1-1"],
                }
            ),
            input_tokens=20,
            output_tokens=16,
            retry_count=0,
            finish_reason="STOP",
        )


class FakePlannerRouter:
    def __init__(self) -> None:
        self.client = FakePlannerClient()

    def get_client(self, profile_id: str, execution_policy=None):
        return self.client


@unittest.skipUnless(DATABASE_URL, "PostgreSQL integration test requires AGENT_ARMY_DATABASE_URL")
class PostgresSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "baseline"], check=True)
        self.store = PostgresGoalStore(DATABASE_URL, self.root / "goals")
        with self.store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE agent_army_goal_requests, agent_army_goals, agent_army_goal_sequences")
        self.service = GoalService(store=self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parallel_same_request_creates_one_goal(self):
        ids: list[str] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()
        barrier = threading.Barrier(6)

        def create() -> None:
            try:
                barrier.wait(timeout=5)
                record = self.service.create_goal("Fix app", self.repo, idempotency_key="same-request")
                with result_lock:
                    ids.append(record.goal_id)
            except BaseException as exc:  # pragma: no cover - captured for assertion
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 1)
        with self.store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM agent_army_goals")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM agent_army_goal_requests")
                self.assertEqual(cur.fetchone()[0], 1)

    def test_stale_transition_is_rejected_by_compare_and_swap(self):
        record = self.service.create_goal("Fix app", self.repo)
        stale = self.service.read_goal(record.goal_id)
        advanced = self.service.update_status(record, "PLANNING", phase="planning")
        self.assertEqual(advanced.status, "PLANNING")
        with self.assertRaises(GoalConcurrencyError):
            self.service.update_status(stale, "PLANNING_FAILED", phase="planning-failed")

    def test_planner_persists_planned_status_through_postgres(self):
        record = self.service.create_goal("Fix app", self.repo)
        planner = GoalPlanner(service=self.service, router=FakePlannerRouter())

        planned, *_ = planner.plan_goal(record.goal_id, "gemini-user-a")

        self.assertEqual(planned.status, "PLANNED")
        self.assertEqual(self.service.read_goal(record.goal_id).status, "PLANNED")
        self.assertTrue((self.store.goal_dir(record.goal_id) / "search_plan.json").exists())
        self.assertTrue((self.store.goal_dir(record.goal_id) / "evidence.json").exists())
        self.assertTrue((self.store.goal_dir(record.goal_id) / "plan.json").exists())
        with self.store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, payload->>'status' FROM agent_army_goals WHERE goal_id = %s",
                    (record.goal_id,),
                )
                self.assertEqual(cur.fetchone(), ("PLANNED", "PLANNED"))

    def test_advisory_lock_serializes_workers(self):
        record = self.service.create_goal("Fix app", self.repo)
        entered: list[float] = []
        exited: list[float] = []
        barrier = threading.Barrier(2)

        def worker(hold: float) -> None:
            barrier.wait(timeout=5)
            with self.store.goal_lock(record.goal_id):
                entered.append(time.monotonic())
                time.sleep(hold)
                exited.append(time.monotonic())

        first = threading.Thread(target=worker, args=(0.35,))
        second = threading.Thread(target=worker, args=(0.05,))
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertEqual(len(entered), 2)
        self.assertEqual(len(exited), 2)
        # The second lock holder cannot enter until the first holder exits.
        self.assertGreaterEqual(entered[1], exited[0] - 0.02)


if __name__ == "__main__":
    unittest.main()
