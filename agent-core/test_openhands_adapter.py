from __future__ import annotations

import json
import subprocess
import tempfile
import shutil
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from goal.builder import BuilderRequest, BuilderResult
from goal.builder_policy import BuilderPolicy
from goal.openhands_adapter import OpenHandsBuilderAdapter, OpenHandsUnavailableError


class _FakeLLM:
    def __init__(self, *args, **kwargs):  # noqa: ANN001
        self.args = args
        self.kwargs = kwargs

    def runtime(self) -> dict[str, int]:
        return {
            "provider_requests": 1,
            "provider_retries": 0,
            "builder_rate_limit_waits": 0,
            "builder_rate_limit_wait_seconds": 0,
            "provider_429_count": 0,
            "provider_503_count": 0,
            "provider_timeout_count": 0,
            "quota_exhausted_count": 0,
            "retry_exhausted": False,
        }

    def completion(self, *args, **kwargs):  # noqa: ANN001
        return types.SimpleNamespace(text="ok")


class OpenHandsAdapterTests(unittest.TestCase):
    def test_execute_routes_into_live_path(self) -> None:
        adapter = OpenHandsBuilderAdapter(BuilderPolicy())
        request = self._make_request(Path("C:/tmp/workspace"))

        with patch("goal.openhands_adapter._package_version", side_effect=["1.0.0", "1.0.0"]), patch.object(
            OpenHandsBuilderAdapter,
            "_execute_live",
            return_value=self._fake_result(),
        ) as live_mock:
            result = adapter.execute(request)

        self.assertEqual(result.status, "BUILT_PENDING_REVIEW")
        live_mock.assert_called_once()

    def test_live_path_enables_terminal_and_uses_configured_mvp_iteration_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            original = (repo / "calculator.py").read_text(encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            for item in repo.iterdir():
                if item.name == ".git":
                    continue
                target = workspace / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)

            fake_agent_state = self._install_fake_openhands_modules()
            request = self._make_request(workspace, target_repo=repo)
            adapter = OpenHandsBuilderAdapter(BuilderPolicy())
            patch_path = workspace / "build.patch"
            patch_path.write_text("diff --git a/calculator.py b/calculator.py\n", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "AGENT_ARMY_OPENHANDS_ONLY": "true",
                    "AGENT_ARMY_OPENHANDS_MAX_ITERATIONS": "2000",
                },
                clear=False,
            ), patch.object(adapter, "_load_profile", return_value={
                "id": "builder",
                "provider": "gemini",
                "model": "gemini/flash",
                "base_url": None,
                "secret_env": None,
            }):
                result = adapter._execute_live(request, sdk_version="1.0.0", tools_version="1.0.0")

            self.assertEqual(result.status, "BUILT_PENDING_REVIEW")
            self.assertTrue(result.openhands_executed)
            self.assertTrue(result.terminal_tool_enabled)
            self.assertEqual(result.changed_files, ["calculator.py"])
            self.assertFalse(result.original_repo_modified)
            self.assertTrue(result.patch_path)
            self.assertTrue(Path(result.patch_path).exists())
            self.assertEqual((repo / "calculator.py").read_text(encoding="utf-8"), original)
            self.assertIn("FileEditorTool", fake_agent_state["tool_names"])
            self.assertIn("TaskTrackerTool", fake_agent_state["tool_names"])
            self.assertIn("TerminalTool", fake_agent_state["tool_names"])
            self.assertEqual(fake_agent_state["max_iterations"], 2000)
            self.assertFalse(fake_agent_state["stuck_detection"])
            self.assertIn("Run every requested verification command", adapter._build_task_prompt(request))

    def test_live_provider_failure_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            workspace = root / "workspace"
            workspace.mkdir()
            self._install_fake_openhands_modules(fail_with=RuntimeError("provider exploded"))
            request = self._make_request(workspace, target_repo=repo)
            adapter = OpenHandsBuilderAdapter(BuilderPolicy())
            with patch.object(adapter, "_load_profile", return_value={
                "id": "builder",
                "provider": "gemini",
                "model": "gemini/flash",
                "base_url": None,
                "secret_env": None,
            }):
                with self.assertRaises(RuntimeError) as ctx:
                    adapter._execute_live(request, sdk_version="1.0.0", tools_version="1.0.0")
            self.assertEqual(str(ctx.exception), "provider exploded")

    def _make_request(self, workspace: Path, target_repo: Path | None = None) -> BuilderRequest:
        target = target_repo or workspace
        return BuilderRequest(
            goal_id="GOAL-TEST",
            goal={"goal_id": "GOAL-TEST", "goal": "Add types"},
            plan={"candidate_files": ["calculator.py"], "acceptance_criteria": ["typed"], "verification": ["python -m unittest test_calculator.py"], "constraints": []},
            review={"final_verdict": "PASS"},
            complexity={"severity": "EASY", "recommended_executor": "openhands"},
            evidence={"summary": {"files_inspected": 1}, "evidence": []},
            mode="relaxed-acceptance",
            builder_profile="builder",
            allowed_files=["calculator.py"],
            forbidden_patterns=[".env"],
            forbidden_areas=["secrets"],
            acceptance_criteria=["typed"],
            verification_commands=["python -m unittest test_calculator.py"],
            constraints=[],
            workspace_path=str(workspace),
            target_repo=str(target),
            allow_new_files=False,
        )

    def _fake_result(self) -> BuilderResult:
        return BuilderResult(
            goal_id="GOAL-TEST",
            status="BUILT_PENDING_REVIEW",
            failure_type=None,
            recommended_executor="openhands",
            changed_files=["calculator.py"],
            unauthorized_files=[],
            patch_path=None,
            patch_size=0,
            verification_status="PASS",
            verification_result={"status": "PASS", "exit_code": 0},
            openhands_executed=True,
            terminal_tool_enabled=False,
            original_repo_modified=False,
            provider_requests=1,
            provider_retries=0,
            builder_rate_limit_waits=0,
            builder_rate_limit_wait_seconds=0.0,
            provider_429_count=0,
            provider_503_count=0,
            provider_timeout_count=0,
            quota_exhausted_count=0,
            retry_exhausted=False,
            preflight_warnings=[],
        )

    def _install_fake_openhands_modules(self, fail_with: Exception | None = None):
        fake_state: dict[str, object] = {
            "tool_names": [],
            "max_iterations": None,
            "stuck_detection": None,
        }

        class FakeTool:
            name = "Tool"

        class FakeFileEditorTool(FakeTool):
            name = "FileEditorTool"

        class FakeTaskTrackerTool(FakeTool):
            name = "TaskTrackerTool"

        class FakeTerminalTool(FakeTool):
            name = "TerminalTool"

        class FakeAgent:
            def __init__(self, llm, tools):  # noqa: ANN001
                fake_state["tool_names"] = [getattr(tool, "name", type(tool).__name__) for tool in tools]
                self.llm = llm

        class FakeWorkspace:
            def __init__(self, working_dir):  # noqa: ANN001
                self.working_dir = Path(working_dir)

        class FakeConversation:
            def __init__(self, agent, workspace, max_iteration_per_run, stuck_detection, delete_on_close):  # noqa: ANN001
                self.workspace = workspace
                fake_state["max_iterations"] = max_iteration_per_run
                fake_state["stuck_detection"] = stuck_detection

            def send_message(self, text):  # noqa: ANN001
                pass

            def run(self):
                if fail_with is not None:
                    raise fail_with
                subprocess.run(["git", "-C", str(self.workspace.working_dir), "init"], check=True, capture_output=True, text=True)
                subprocess.run(["git", "-C", str(self.workspace.working_dir), "config", "user.email", "test@example.com"], check=True, capture_output=True, text=True)
                subprocess.run(["git", "-C", str(self.workspace.working_dir), "config", "user.name", "Test User"], check=True, capture_output=True, text=True)
                subprocess.run(["git", "-C", str(self.workspace.working_dir), "add", "."], check=True, capture_output=True, text=True)
                (self.workspace.working_dir / "calculator.py").write_text(
                    "def add(a: int, b: int) -> int:\n    return a + b\n",
                    encoding="utf-8",
                )

            def close(self):
                pass

        sdk_agent = types.ModuleType("openhands.sdk.agent")
        sdk_agent.Agent = FakeAgent
        sdk_conversation = types.ModuleType("openhands.sdk.conversation")
        sdk_conversation.Conversation = FakeConversation
        sdk_llm = types.ModuleType("openhands.sdk.llm")
        sdk_llm.LLM = _FakeLLM
        sdk_workspace = types.ModuleType("openhands.sdk.workspace.local")
        sdk_workspace.LocalWorkspace = FakeWorkspace
        sdk_tool_spec = types.ModuleType("openhands.sdk.tool.spec")
        sdk_tool_spec.Tool = FakeTool
        tools_file_editor = types.ModuleType("openhands.tools.file_editor")
        tools_file_editor.FileEditorTool = FakeFileEditorTool
        tools_task_tracker = types.ModuleType("openhands.tools.task_tracker")
        tools_task_tracker.TaskTrackerTool = FakeTaskTrackerTool
        tools_terminal = types.ModuleType("openhands.tools.terminal")
        tools_terminal.TerminalTool = FakeTerminalTool
        tools_preset_default = types.ModuleType("openhands.tools.preset.default")
        tools_preset_default.get_default_tools = lambda enable_browser=False: [FakeFileEditorTool(), FakeTaskTrackerTool(), FakeTerminalTool()]

        patcher = patch.dict(
            sys.modules,
            {
                "openhands.sdk.agent": sdk_agent,
                "openhands.sdk.conversation": sdk_conversation,
                "openhands.sdk.llm": sdk_llm,
                "openhands.sdk.workspace.local": sdk_workspace,
                "openhands.sdk.tool.spec": sdk_tool_spec,
                "openhands.tools.file_editor": tools_file_editor,
                "openhands.tools.task_tracker": tools_task_tracker,
                "openhands.tools.terminal": tools_terminal,
                "openhands.tools.preset.default": tools_preset_default,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake_state

    def _init_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init")
        self._git(path, "config", "user.email", "test@example.com")
        self._git(path, "config", "user.name", "Test User")
        (path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (path / "test_calculator.py").write_text(
            "import unittest\n\nclass TestCalculator(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self._git(path, "add", ".")
        self._git(path, "commit", "-m", "init")
        return path

    def _git(self, path: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
