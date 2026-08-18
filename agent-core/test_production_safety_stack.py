from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from goal.openhands_docker_hardened import runtime_exclude_patterns
from goal.service import GoalService
from goal.store import GoalConcurrencyError, GoalStore
from goal.verification_sandbox import run_docker_verification_suite
from llm.client import OpenAICompatibleClient
from llm.router import LLMRouter
from observability import init_observability, observability_health
from run_builder import _cleanup_staging_worktree


class ProductionSafetyStackTests(unittest.TestCase):
    def _repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
        return repo

    def test_idempotency_key_reuses_same_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            service = GoalService(store=GoalStore(root / "goals"))
            first = service.create_goal("Fix app", repo, idempotency_key="request-123")
            second = service.create_goal("Fix app", repo, idempotency_key="request-123")
            self.assertEqual(first.goal_id, second.goal_id)
            self.assertEqual(service.store.list_goal_ids(), [first.goal_id])

    def test_parallel_goal_id_allocation_is_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            service = GoalService(store=GoalStore(root / "goals"))
            ids: list[str] = []
            errors: list[BaseException] = []
            lock = threading.Lock()

            def create(index: int) -> None:
                try:
                    record = service.create_goal(f"Task {index}", repo)
                    with lock:
                        ids.append(record.goal_id)
                except BaseException as exc:  # pragma: no cover - assertion captures thread errors
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(ids), 8)
            self.assertEqual(len(set(ids)), 8)

    def test_stale_state_transition_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            service = GoalService(store=GoalStore(root / "goals"))
            record = service.create_goal("Fix app", repo)
            stale = service.read_goal(record.goal_id)
            service.update_status(record, "PLANNING", phase="planning")
            with self.assertRaises(GoalConcurrencyError):
                service.update_status(stale, "PLANNING_FAILED", phase="planning-failed")

    def test_litellm_proxy_does_not_require_direct_provider_secret(self):
        env = {
            "AGENT_ARMY_LITELLM_ENABLED": "true",
            "AGENT_ARMY_LITELLM_PROXY_URL": "http://127.0.0.1:4000/v1",
            "AGENT_ARMY_LITELLM_API_KEY": "test-proxy-key",
            "GEMINI_USER_A_KEY": "",
        }
        core = Path(__file__).resolve().parent
        with patch.dict(os.environ, env, clear=False):
            router = LLMRouter(config_path=core / "config" / "profiles.yaml", env_path=core / ".env.missing")
            client = router.get_client("gemini-user-a")
        self.assertIsInstance(client, OpenAICompatibleClient)
        self.assertEqual(client.model, "gemini-user-a")
        self.assertEqual(client.base_url, "http://127.0.0.1:4000/v1")

    def test_verification_sandbox_fails_closed_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("goal.verification_sandbox.shutil.which", return_value=None):
                result = run_docker_verification_suite(["python -V"], tmp)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["failure_code"], "DOCKER_NOT_INSTALLED")

    def test_builder_worktree_cleanup_removes_staging_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            workspace = root / "staging"
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(workspace)],
                check=True,
                capture_output=True,
                text=True,
            )
            (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            request = SimpleNamespace(target_repo=str(repo), workspace_path=str(workspace))
            with patch.dict(os.environ, {"AGENT_ARMY_KEEP_WORKSPACE": "false"}, clear=False):
                status = _cleanup_staging_worktree(request)
            self.assertEqual(status, "REMOVED")
            self.assertFalse(workspace.exists())
            listing = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertNotIn(str(workspace), listing)

    def test_openhands_runtime_metadata_is_ignored_by_staging_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            patterns = runtime_exclude_patterns(repo, ["app.py"])
            self.assertIn("conversations/", patterns)
            self.assertIn("bash_events/", patterns)
            self.assertIn("__pycache__/", patterns)
            self.assertIn("*.pyc", patterns)

            exclude_raw = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            exclude_path = Path(exclude_raw)
            if not exclude_path.is_absolute():
                exclude_path = repo / exclude_path
            with exclude_path.open("a", encoding="utf-8") as handle:
                handle.write("\n" + "\n".join(patterns) + "\n")

            (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            for relative in (
                ".openhands/state.json",
                "conversations/session.json",
                "bash_events/event.json",
                "__pycache__/app.cpython-313.pyc",
            ):
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("runtime\n", encoding="utf-8")

            subprocess.run(["git", "-C", str(repo), "add", "-N", "."], check=True)
            changed = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=ACDMRTUXB", "--", "."],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertEqual(changed, ["app.py"])

    def test_openhands_runtime_filter_does_not_hide_legitimate_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            conversations = repo / "conversations"
            conversations.mkdir()
            (conversations / "project.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "conversations/project.json"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "track project conversations"], check=True)

            patterns = runtime_exclude_patterns(repo, ["app.py"])
            self.assertNotIn("conversations/", patterns)
            self.assertIn("bash_events/", patterns)

    def test_observability_health_is_explicit_when_disabled(self):
        env = {
            "AGENT_ARMY_SENTRY_ENABLED": "false",
            "AGENT_ARMY_LANGFUSE_ENABLED": "false",
            "AGENT_ARMY_OTEL_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            init_observability()
            health = observability_health()
        self.assertFalse(health["sentry"]["configured"])
        self.assertFalse(health["langfuse"]["configured"])
        self.assertFalse(health["opentelemetry"]["configured"])


if __name__ == "__main__":
    unittest.main()
