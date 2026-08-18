from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class ObservabilitySmokeTests(unittest.TestCase):
    def test_observability_disabled_is_noop(self) -> None:
        from observability import observe_run

        with patch.dict(os.environ, {"AGENT_ARMY_LANGFUSE_ENABLED": "false"}, clear=False):
            seen = []
            with observe_run("smoke", metadata={"x": "y"}):
                seen.append("ran")
            self.assertEqual(seen, ["ran"])

    def test_sentry_without_dsn_stays_disabled(self) -> None:
        from observability import init_sentry

        with patch.dict(
            os.environ,
            {"AGENT_ARMY_SENTRY_ENABLED": "true", "SENTRY_DSN": ""},
            clear=False,
        ):
            self.assertFalse(init_sentry())


class TemporalSmokeTests(unittest.TestCase):
    def test_temporal_workflow_imports(self) -> None:
        from orchestration.temporal_flow import DurableGoalWorkflow, run_pipeline_stage

        self.assertTrue(callable(run_pipeline_stage))
        self.assertIsNotNone(DurableGoalWorkflow)


if __name__ == "__main__":
    unittest.main()
