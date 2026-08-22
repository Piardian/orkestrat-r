from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from goal.verification_sandbox import run_docker_commands_in_workspace


class DockerVerificationSessionTests(unittest.TestCase):
    def test_commands_share_one_container_and_are_sent_over_stdin(self) -> None:
        commands = [
            "npm test",
            "node src/server.js &",
            "curl http://localhost:3000/health",
            "curl http://localhost:3000/demo",
            "pkill -f src/server.js",
        ]
        calls: list[tuple[list[str], bytes | None]] = []

        def fake_run(argv, **kwargs):  # noqa: ANN001
            args = [str(item) for item in argv]
            calls.append((args, kwargs.get("input")))
            stdout = ""
            if args[:2] == ["docker", "run"]:
                stdout = "container-id\n"
            elif args[:2] == ["docker", "exec"] and "curl" in str(kwargs.get("input")):
                stdout = '{"ok":true}\n'
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "goal.verification_sandbox.subprocess.run",
            side_effect=fake_run,
        ), patch.dict(
            os.environ,
            {"AGENT_ARMY_VERIFY_BACKGROUND_GRACE_SECONDS": "0"},
            clear=False,
        ):
            result = run_docker_commands_in_workspace(
                commands,
                Path(tmp),
                image="verify-image",
                timeout=30.0,
            )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["command_results"]), len(commands))

        run_calls = [call for call in calls if call[0][:2] == ["docker", "run"]]
        exec_calls = [call for call in calls if call[0][:2] == ["docker", "exec"]]
        cleanup_calls = [call for call in calls if call[0][:3] == ["docker", "rm", "-f"]]
        self.assertEqual(len(run_calls), 1)
        self.assertEqual(len(exec_calls), len(commands))
        self.assertEqual(len(cleanup_calls), 1)

        container_name = run_calls[0][0][run_calls[0][0].index("--name") + 1]
        self.assertTrue(all(call[0][3] == container_name for call in exec_calls))
        self.assertEqual(exec_calls[0][1], b"npm test\n")
        self.assertEqual(exec_calls[1][0][2], "-d")
        self.assertIsNone(exec_calls[1][1])
        self.assertEqual(exec_calls[1][0][-1], "node src/server.js")
        self.assertEqual(exec_calls[2][1], b"curl http://localhost:3000/health\n")
        self.assertEqual(exec_calls[3][1], b"curl http://localhost:3000/demo\n")
        self.assertEqual(exec_calls[4][1], b"pkill -f src/server.js\n")
        self.assertNotIn("src/server.js", " ".join(exec_calls[4][0]))
        self.assertEqual(cleanup_calls[0][0][-1], container_name)

    def test_first_failed_command_stops_suite_and_removes_container(self) -> None:
        calls: list[list[str]] = []
        exec_count = 0

        def fake_run(argv, **kwargs):  # noqa: ANN001
            nonlocal exec_count
            args = [str(item) for item in argv]
            calls.append(args)
            if args[:2] == ["docker", "exec"]:
                exec_count += 1
                return subprocess.CompletedProcess(
                    args,
                    0 if exec_count == 1 else 7,
                    stdout="",
                    stderr="connection failed" if exec_count == 2 else "",
                )
            return subprocess.CompletedProcess(args, 0, stdout="container-id\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "goal.verification_sandbox.subprocess.run",
            side_effect=fake_run,
        ):
            result = run_docker_commands_in_workspace(
                ["npm test", "curl http://localhost:3000/health", "never-run"],
                Path(tmp),
                image="verify-image",
                timeout=30.0,
            )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["command"], "curl http://localhost:3000/health")
        self.assertEqual(len(result["command_results"]), 2)
        self.assertEqual(exec_count, 2)
        self.assertEqual(len([call for call in calls if call[:3] == ["docker", "rm", "-f"]]), 1)


if __name__ == "__main__":
    unittest.main()
