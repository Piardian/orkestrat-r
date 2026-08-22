from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from goal.runtime_policy import (
    openhands_max_iterations,
    openhands_only_mode,
    openhands_stuck_detection_enabled,
    openhands_terminal_enabled,
)


class RuntimePolicyTests(unittest.TestCase):
    def test_mvp_defaults_to_openhands_only_with_terminal_and_no_stuck_detection(self) -> None:
        keys = [
            "AGENT_ARMY_OPENHANDS_ONLY",
            "AGENT_ARMY_FORCE_OPENHANDS",
            "AGENT_ARMY_CODEX_ENABLED",
            "AGENT_ARMY_COMPLEXITY_GATE_ENABLED",
            "AGENT_ARMY_REQUIRE_CODEX_FOR_COMPLEXITY",
            "AGENT_ARMY_OPENHANDS_TERMINAL_ENABLED",
            "AGENT_ARMY_OPENHANDS_STUCK_DETECTION",
        ]
        clean = {key: os.environ[key] for key in os.environ if key not in keys}
        with patch.dict(os.environ, clean, clear=True):
            self.assertTrue(openhands_only_mode())
            self.assertTrue(openhands_terminal_enabled())
            self.assertFalse(openhands_stuck_detection_enabled())

    def test_existing_force_and_iteration_aliases_are_honored(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_ARMY_FORCE_OPENHANDS": "true",
                "AGENT_ARMY_BUILDER_MAX_ITERATIONS": "2000",
            },
            clear=True,
        ):
            self.assertTrue(openhands_only_mode())
            self.assertEqual(openhands_max_iterations(10_000), 2000)

    def test_canonical_settings_can_restore_gated_mode(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_ARMY_OPENHANDS_ONLY": "false",
                "AGENT_ARMY_OPENHANDS_TERMINAL_ENABLED": "false",
                "AGENT_ARMY_OPENHANDS_STUCK_DETECTION": "true",
            },
            clear=True,
        ):
            self.assertFalse(openhands_only_mode())
            self.assertFalse(openhands_terminal_enabled())
            self.assertTrue(openhands_stuck_detection_enabled())


if __name__ == "__main__":
    unittest.main()
