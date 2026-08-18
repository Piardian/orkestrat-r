from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import unittest

from goal import GoalService, GoalStore
from goal.resume_service import GoalResumeService
from goal.status_service import GoalStatusService


class Phase8GoalHistoryTests(unittest.TestCase):
    def test_goals_status_history_and_safe_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service = GoalService(GoalStore(root / "runtime" / "goals"))
            created = service.create_goal("Improve goal visibility", repo)
            planning = service.update_status(created, "PLANNING", phase="planning", note="started")
            planned = service.update_status(planning, "PLANNED", phase="planned", note="ready")
            reviewing = service.update_status(planned, "REVIEWING", phase="reviewing", note="reviewing")
            approved = service.update_status(reviewing, "APPROVED", phase="approved", note="approved")

            status_service = GoalStatusService(service)
            snap = status_service.snapshot(approved.goal_id)
            self.assertEqual(snap.state, "APPROVED")
            self.assertIn("COMPLEXITY", snap.completed)
            self.assertTrue(status_service.history(approved.goal_id))

            goals = status_service.list_goals()
            self.assertEqual(goals[0].goal_id, approved.goal_id)

            resume = GoalResumeService(service, runtime_root=root / "runtime")
            dry = resume.resume(approved.goal_id)
            self.assertFalse(dry.executed)
            self.assertIn("Dry-run", dry.message)

    def test_waiting_codex_and_ready_to_apply_are_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service = GoalService(GoalStore(root / "runtime" / "goals"))
            created = service.create_goal("Manual states", repo)
            planning = service.update_status(created, "PLANNING", phase="planning")
            planned = service.update_status(planning, "PLANNED", phase="planned")
            reviewing = service.update_status(planned, "REVIEWING", phase="reviewing")
            approved = service.update_status(reviewing, "APPROVED", phase="approved")
            assessing = service.update_status(approved, "COMPLEXITY_ASSESSING", phase="complexity-assessing")
            codex_required = service.update_status(assessing, "CODEX_REQUIRED", phase="complexity-assessed")
            waiting = service.update_status(codex_required, "WAITING_CODEX", phase="codex-waiting")
            resume = GoalResumeService(service, runtime_root=root / "runtime")
            waiting_result = resume.resume(waiting.goal_id)
            self.assertFalse(waiting_result.executed)
            self.assertIn("Codex response is waiting", waiting_result.message)

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
