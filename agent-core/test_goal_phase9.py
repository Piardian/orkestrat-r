from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goal import GoalMetricsService, GoalService, GoalStore


class Phase9MetricsTests(unittest.TestCase):
    def test_metrics_refresh_and_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            runtime = root / "runtime"
            service = GoalService(GoalStore(runtime / "goals"))
            record = service.create_goal("Track metrics", repo)
            planned = service.update_status(record, "PLANNING", phase="planning", note="start")
            service.update_status(planned, "PLANNED", phase="planned", note="done")

            metrics_service = GoalMetricsService(service, runtime_root=runtime)
            goal_metrics = metrics_service.refresh_goal(record.goal_id)
            self.assertEqual(goal_metrics.goal_id, record.goal_id)
            self.assertTrue((runtime / "goals" / record.goal_id / "metrics.json").exists())

            rebuilt = metrics_service.load_goal_metrics(record.goal_id)
            self.assertIsNotNone(rebuilt)
            self.assertEqual(rebuilt["goal_id"], record.goal_id)
            self.assertEqual(rebuilt["llm"]["logical_calls"], 0)

            global_metrics = metrics_service.refresh_all()
            self.assertEqual(global_metrics["goals_total"], 1)
            self.assertEqual(global_metrics["llm_logical_calls"], 0)
            self.assertTrue((runtime / "metrics" / "metrics.json").exists())

    def test_goal_ctl_metrics_command_prints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            runtime_goals = root / "runtime" / "goals"
            service = GoalService(GoalStore(runtime_goals))
            record = service.create_goal("CLI metrics", repo)
            service.update_status(record, "PLANNING", phase="planning")
            service.update_status(service.read_goal(record.goal_id), "PLANNED", phase="planned")

            goal_ctl = ROOT / "goal_ctl.py"
            result = subprocess.run(
                [sys.executable, str(goal_ctl), "metrics", "--goal-id", record.goal_id, "--runtime-dir", str(runtime_goals)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("GOAL METRICS", result.stdout)
            self.assertIn(record.goal_id, result.stdout)

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
