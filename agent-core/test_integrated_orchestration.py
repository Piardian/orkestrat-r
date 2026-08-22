from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from orchestration.engine import GoalPipelineEngine, PipelineRequest, resolve_openhands_python


class _Record:
    def __init__(self, goal_id: str, status: str) -> None:
        self.goal_id = goal_id
        self.status = status


class _Store:
    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def list_goal_ids(self) -> list[str]:
        return list(self._ids)


class _CompletedService:
    def __init__(self, goal_id: str) -> None:
        self.record = _Record(goal_id, "COMPLETED")
        self.store = _Store([goal_id])

    def read_goal(self, goal_id: str) -> _Record:
        if goal_id != self.record.goal_id:
            raise FileNotFoundError(goal_id)
        return self.record


class _MutableService(_CompletedService):
    def update_status(self, record: _Record, status: str, **kwargs) -> _Record:  # noqa: ANN003
        self.record = _Record(record.goal_id, status)
        return self.record


class IntegratedOrchestrationTests(unittest.TestCase):
    def test_native_runner_respects_existing_terminal_state(self) -> None:
        goal_id = "GOAL-20260817-0001"
        service = _CompletedService(goal_id)
        engine = GoalPipelineEngine(
            PipelineRequest(goal_id=goal_id, auto_apply=False),
            service=service,  # type: ignore[arg-type]
        )

        result = engine.run_native()

        self.assertEqual(result.goal_id, goal_id)
        self.assertEqual(result.state, "COMPLETED")
        self.assertFalse(result.applied)
        self.assertIsNone(result.next_action)
        self.assertTrue(any(item["action"] == "manual-gate" for item in result.stages))

    def test_openhands_python_prefers_explicit_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            python_path = Path(tmp) / "python-custom.exe"
            python_path.write_text("", encoding="utf-8")
            resolved = resolve_openhands_python(str(python_path), Path(tmp))
            self.assertEqual(Path(resolved), python_path.resolve())

    def test_legacy_codex_state_is_redirected_to_openhands_builder(self) -> None:
        goal_id = "GOAL-20260817-0002"
        service = _MutableService(goal_id)
        service.record = _Record(goal_id, "CODEX_REQUIRED")
        engine = GoalPipelineEngine(PipelineRequest(goal_id=goal_id), service=service)  # type: ignore[arg-type]
        completed = types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, {"AGENT_ARMY_OPENHANDS_ONLY": "true"}, clear=False), patch(
            "orchestration.engine.resolve_openhands_python",
            return_value=sys.executable,
        ), patch("orchestration.engine.subprocess.run", return_value=completed) as run_mock:
            state = engine.build(goal_id)

        self.assertEqual(state, "READY_FOR_OPENHANDS")
        self.assertEqual(service.record.status, "READY_FOR_OPENHANDS")
        run_mock.assert_called_once()

    def test_openclaw_dispatch_never_forwards_auto_apply(self) -> None:
        script = Path(__file__).parent / "openclaw" / "skills" / "agent-army" / "scripts" / "dispatch.py"
        spec = importlib.util.spec_from_file_location("agent_army_dispatch", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            core = tmp_path / "agent-core"
            core.mkdir()
            (core / "run_integrated.py").write_text("print('stub')", encoding="utf-8")
            repo = tmp_path / "repo"
            repo.mkdir()
            request = tmp_path / "request.json"
            request.write_text(
                json.dumps({"repo": str(repo), "task": "safe task", "auto_apply": True}),
                encoding="utf-8",
            )

            captured: dict[str, object] = {}

            class _Proc:
                returncode = 0
                stdout = "{}\n"
                stderr = ""

            def fake_run(command, **kwargs):
                captured["command"] = list(command)
                return _Proc()

            argv = ["dispatch.py", "--request-file", str(request)]
            with patch.dict(os.environ, {"AGENT_ARMY_CORE_DIR": str(core)}, clear=False), patch.object(
                sys, "argv", argv
            ), patch.object(module.subprocess, "run", side_effect=fake_run):
                rc = module.main()

            self.assertEqual(rc, 0)
            command = captured["command"]
            self.assertIsInstance(command, list)
            self.assertNotIn("--auto-apply", command)
            self.assertIn("--orchestrator", command)
            self.assertIn("crewai", command)


if __name__ == "__main__":
    unittest.main()
