from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from goal.model import GoalRecord
from goal.sandboxed_finalize import SandboxedFinalReviewService
from goal.service import GoalService
from goal.store import GoalStore
from orchestration.temporal_flow import require_temporal_postgres


class ProbeFinalReviewService(SandboxedFinalReviewService):
    def __init__(self, *args, fail_post_promotion: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_post_promotion = fail_post_promotion
        self.verification_calls = 0

    def _run_verification(self, workspace: Path, commands: list[str]):
        self.verification_calls += 1
        content = (Path(workspace) / "app.py").read_text(encoding="utf-8")
        passed = "VALUE = 2" in content
        if self.fail_post_promotion and self.verification_calls >= 2:
            passed = False
        return {
            "status": "PASS" if passed else "FAIL",
            "exit_code": 0 if passed else 1,
            "command": "probe",
            "stdout": content,
            "stderr": "" if passed else "forced post-promotion failure",
            "duration_ms": 1,
            "failure_code": None if passed else "FORCED_FAILURE",
            "command_results": [],
            "reason": "" if passed else "FORCED_FAILURE",
        }


class TransactionalApplyTests(unittest.TestCase):
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
        self.base_commit = self._git("rev-parse", "HEAD")

        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.patch_text = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--binary", "--", "app.py"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--", "app.py"], check=True)

        self.store = GoalStore(self.root / "goals")
        self.service = GoalService(store=self.store)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.record = GoalRecord(
            goal_id="GOAL-20260818-0001",
            goal="Set VALUE to 2",
            repo=str(self.repo),
            status="READY_TO_APPLY",
            created_at=now,
            updated_at=now,
            phase="ready-to-apply",
            utc_timestamp=True,
            goal_type="CODE_MODIFICATION",
            notes=[],
        )
        self.store.save(self.record)
        goal_dir = self.store.goal_dir(self.record.goal_id)
        (goal_dir / "build.patch").write_text(self.patch_text, encoding="utf-8")
        self.store.save_plan(
            self.record.goal_id,
            "build_request.json",
            {
                "target_repo": str(self.repo),
                "allowed_files": ["app.py"],
                "verification_commands": ["probe"],
            },
        )
        self.store.save_plan(
            self.record.goal_id,
            "apply_manifest.json",
            {
                "goal_id": self.record.goal_id,
                "patch_sha256": hashlib.sha256(self.patch_text.encode("utf-8")).hexdigest(),
                "base_commit": self.base_commit,
            },
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    @patch("goal.sandboxed_finalize.GoalMetricsService.refresh_goal", return_value=None)
    def test_verified_patch_is_promoted_as_git_commit(self, _metrics) -> None:
        final = ProbeFinalReviewService(service=self.service, runtime_root=self.root / "runtime")
        record, result = final.apply(self.record.goal_id, explicit_apply=True)
        self.assertEqual(record.status, "COMPLETED")
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["transactional"])
        self.assertIn("VALUE = 2", (self.repo / "app.py").read_text(encoding="utf-8"))
        self.assertNotEqual(self._git("rev-parse", "HEAD"), self.base_commit)
        transaction = json.loads((self.store.goal_dir(self.record.goal_id) / "apply_transaction.json").read_text(encoding="utf-8"))
        self.assertEqual(transaction["status"], "COMPLETED")
        self.assertEqual(transaction["promoted_commit"], self._git("rev-parse", "HEAD"))

    @patch("goal.sandboxed_finalize.GoalMetricsService.refresh_goal", return_value=None)
    def test_failed_post_promotion_verification_rolls_repo_back(self, _metrics) -> None:
        final = ProbeFinalReviewService(
            service=self.service,
            runtime_root=self.root / "runtime",
            fail_post_promotion=True,
        )
        record, result = final.apply(self.record.goal_id, explicit_apply=True)
        self.assertEqual(record.status, "POST_APPLY_VERIFICATION_FAILED")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["rolled_back"])
        self.assertEqual(self._git("rev-parse", "HEAD"), self.base_commit)
        self.assertIn("VALUE = 1", (self.repo / "app.py").read_text(encoding="utf-8"))
        transaction = json.loads((self.store.goal_dir(self.record.goal_id) / "apply_transaction.json").read_text(encoding="utf-8"))
        self.assertEqual(transaction["status"], "ROLLED_BACK")

    @patch("goal.sandboxed_finalize.GoalMetricsService.refresh_goal", return_value=None)
    def test_applying_state_resumes_after_commit_was_promoted(self, _metrics) -> None:
        final = ProbeFinalReviewService(service=self.service, runtime_root=self.root / "runtime")
        patch_path = self.store.goal_dir(self.record.goal_id) / "build.patch"
        staged_commit, verification = final._stage_verified_commit(
            self.record.goal_id,
            self.repo,
            self.base_commit,
            patch_path,
            hashlib.sha256(self.patch_text.encode("utf-8")).hexdigest(),
            ["probe"],
        )
        self.assertEqual(verification["status"], "PASS")
        self.assertTrue(staged_commit)
        self.store.save_plan(
            self.record.goal_id,
            "apply_transaction.json",
            {
                "goal_id": self.record.goal_id,
                "base_commit": self.base_commit,
                "staged_commit": staged_commit,
                "status": "VERIFIED",
            },
        )
        applying = self.service.update_status(self.record, "APPLYING", phase="applying")
        subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--ff-only", str(staged_commit)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(applying.status, "APPLYING")

        resumed, result = final.apply(self.record.goal_id, explicit_apply=True)
        self.assertEqual(resumed.status, "COMPLETED")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self._git("rev-parse", "HEAD"), staged_commit)


class TemporalDurabilityGuardTests(unittest.TestCase):
    def test_temporal_rejects_filesystem_state_by_default(self) -> None:
        env = {
            "AGENT_ARMY_TEMPORAL_REQUIRE_POSTGRES": "true",
            "AGENT_ARMY_STATE_BACKEND": "file",
            "AGENT_ARMY_DATABASE_URL": "",
            "DATABASE_URL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                require_temporal_postgres()

    def test_temporal_dev_opt_out_is_explicit(self) -> None:
        env = {
            "AGENT_ARMY_TEMPORAL_REQUIRE_POSTGRES": "false",
            "AGENT_ARMY_STATE_BACKEND": "file",
            "AGENT_ARMY_DATABASE_URL": "",
            "DATABASE_URL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            require_temporal_postgres()


if __name__ == "__main__":
    unittest.main()
