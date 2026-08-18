from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from goal import GoalService, GoalStore, parse_goal_command
from goal_cli import _safe_goal_preview


class GoalTests(unittest.TestCase):
    def test_valid_goal_creates_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(Path(tmp) / "goals"))
            record = service.create_goal("Build login", repo)
            self.assertEqual(record.status, "CREATED")
            self.assertTrue(record.goal_id.startswith("GOAL-"))

    def test_blank_goal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(Path(tmp) / "goals"))
            with self.assertRaises(ValueError):
                service.create_goal("   ", repo)

    def test_invalid_repo_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GoalService(GoalStore(Path(tmp) / "goals"))
            with self.assertRaises(ValueError):
                service.create_goal("Build login", Path(tmp) / "missing")

    def test_unique_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(Path(tmp) / "goals"))
            first = service.create_goal("Build login", repo)
            second = service.create_goal("Build profile", repo)
            self.assertNotEqual(first.goal_id, second.goal_id)

    def test_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            store = GoalStore(Path(tmp) / "goals")
            service = GoalService(store)
            record = service.create_goal("Build login", repo)
            loaded = store.load(record.goal_id)
            self.assertEqual(loaded.goal, "Build login")
            self.assertEqual(loaded.repo, str(repo.resolve()))

    def test_parser_handles_quotes(self) -> None:
        self.assertEqual(parse_goal_command('/goal "Build JWT login"'), "Build JWT login")

    def test_parser_rejects_non_goal(self) -> None:
        self.assertIsNone(parse_goal_command("hello"))

    def test_secret_like_text_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            store = GoalStore(Path(tmp) / "goals")
            service = GoalService(store)
            record = service.create_goal("Investigate token-like text: nvapi-abc123", repo)
            saved = json.loads(store.path_for(record.goal_id).read_text(encoding="utf-8"))
            self.assertIn("nvapi-abc123", saved["goal"])

    def test_safe_goal_preview_redacts_tokens(self) -> None:
        preview = _safe_goal_preview("Investigate nvapi-abc123 and sk-abcdef1234567890")
        self.assertNotIn("nvapi-abc123", preview)
        self.assertNotIn("sk-abcdef1234567890", preview)

    def test_long_goal_accepted_lengths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(Path(tmp) / "goals"))

            # 500 chars
            goal_500 = "A" * 500
            rec_500 = service.create_goal(goal_500, repo)
            self.assertEqual(len(rec_500.goal), 500)

            # 2,000 chars
            goal_2000 = "B" * 2000
            rec_2000 = service.create_goal(goal_2000, repo)
            self.assertEqual(len(rec_2000.goal), 2000)

            # 10,000 chars
            goal_10000 = "C" * 10000
            rec_10000 = service.create_goal(goal_10000, repo)
            self.assertEqual(len(rec_10000.goal), 10000)

    def test_empty_goal_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(Path(tmp) / "goals"))
            with self.assertRaisesRegex(ValueError, "Goal cannot be empty"):
                service.create_goal("", repo)
            with self.assertRaisesRegex(ValueError, "Goal cannot be empty"):
                service.create_goal("   \n\t  ", repo)


if __name__ == "__main__":
    unittest.main()
