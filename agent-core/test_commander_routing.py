from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from llm.client import BaseLLMClient, LLMResponse
from goal import GoalPlanner, GoalService, GoalStore
from registry import ProfileRegistry
from routing import load_commander_routing, resolve_commander_profile


class FakeCommanderClient(BaseLLMClient):
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str, **options):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                text='{"task":"search","search_terms":["calculator.py","add"],"max_files":3,"max_lines_per_file":40}',
                input_tokens=5,
                output_tokens=5,
                finish_reason="STOP",
            )
        return LLMResponse(
            text='{"plan_version":1,"goal_id":"GOAL-TEST","objective":"Add type hints","summary":"Do it","tasks":[],"candidate_files":["calculator.py"],"acceptance_criteria":["OK"],"verification":["pytest"],"risks":[],"constraints":[],"patch_expected":true,"uncertainties":[],"evidence_refs":["calculator.py:1-2"]}',
            input_tokens=5,
            output_tokens=5,
            finish_reason="STOP",
        )


class RecordingRouter:
    def __init__(self, client: BaseLLMClient) -> None:
        self.client = client
        self.profile_ids: list[str] = []

    def get_client(self, profile_id: str):
        self.profile_ids.append(profile_id)
        return self.client


class CommanderRoutingTests(unittest.TestCase):
    def test_default_commander_profile_is_gemini(self) -> None:
        routing = load_commander_routing(Path("config") / "army.yaml")
        self.assertEqual(routing.default_profile, "gemini-commander-main")
        self.assertEqual(routing.fallback_profile, "nemotron-main")
        self.assertFalse(routing.automatic_fallback)
        self.assertEqual(resolve_commander_profile(None, Path("config") / "army.yaml"), "gemini-commander-main")

    def test_registry_contains_commander_and_nemotron_profiles(self) -> None:
        registry = ProfileRegistry(Path("config") / "profiles.yaml")
        commander = registry.get("gemini-commander-main")
        nemotron = registry.get("nemotron-main")
        self.assertEqual(commander.provider, "gemini")
        self.assertEqual(commander.model, "gemini/gemini-3.1-flash-lite")
        self.assertEqual(commander.secret_env, "GEMINI_USER_A_KEY")
        self.assertEqual(nemotron.provider, "openai-compatible")
        self.assertEqual(nemotron.model, "nvidia/nemotron-3-super-120b-a12b")

    def test_worker_profiles_use_gemini_3_1_flash_lite(self) -> None:
        registry = ProfileRegistry(Path("config") / "profiles.yaml")
        self.assertEqual(registry.get("gemini-user-a").model, "gemini/gemini-3.1-flash-lite")
        self.assertEqual(registry.get("gemini-user-b").model, "gemini/gemini-3.1-flash-lite")
        self.assertEqual(registry.get("gemini-user-c").model, "gemini/gemini-3.1-flash-lite")
        self.assertEqual(registry.get("gemini-user-d").model, "gemini/gemini-3.1-flash-lite")

    def test_override_profile_is_respected(self) -> None:
        self.assertEqual(resolve_commander_profile("nemotron-main", Path("config") / "army.yaml"), "nemotron-main")

    def test_goal_planner_uses_default_commander_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (repo / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Add type hints to calculator.py", repo)
            router = RecordingRouter(FakeCommanderClient())
            planner = GoalPlanner(service=service, router=router)
            planner.plan_goal(created.goal_id)
            self.assertEqual(router.profile_ids[0], "gemini-commander-main")

    def test_goal_planner_respects_manual_nemotron_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (repo / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Add type hints to calculator.py", repo)
            router = RecordingRouter(FakeCommanderClient())
            planner = GoalPlanner(service=service, router=router)
            planner.plan_goal(created.goal_id, "nemotron-main")
            self.assertEqual(router.profile_ids[0], "nemotron-main")


if __name__ == "__main__":
    unittest.main()
