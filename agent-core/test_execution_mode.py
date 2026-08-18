from __future__ import annotations

import unittest
from unittest.mock import patch

from llm.execution import ExecutionModePolicy, load_execution_policy, resolve_output_tokens


class ExecutionModeTests(unittest.TestCase):
    def test_relaxed_acceptance_policy_values(self) -> None:
        config = {
            "execution_modes": {
                "default": {
                    "output_tokens": "configured",
                    "truncation_regenerations": 1,
                    "json_repairs": 1,
                    "provider_retries": 2,
                    "retry_wait_max_seconds": 60,
                    "stage_timeout_seconds": 600,
                    "max_provider_requests_per_stage": 10,
                },
                "relaxed-acceptance": {
                    "output_tokens": "provider_max",
                    "truncation_regenerations": 3,
                    "json_repairs": 2,
                    "provider_retries": 5,
                    "retry_wait_max_seconds": 120,
                    "stage_timeout_seconds": 900,
                    "max_provider_requests_per_stage": 15,
                },
            }
        }
        policy = load_execution_policy(config, "relaxed-acceptance")
        self.assertEqual(policy.output_tokens, "provider_max")
        self.assertEqual(policy.truncation_regenerations, 3)
        self.assertEqual(policy.json_repairs, 2)
        self.assertEqual(policy.provider_retries, 5)
        self.assertEqual(policy.retry_wait_max_seconds, 120)
        self.assertEqual(policy.stage_timeout_seconds, 900)
        self.assertEqual(policy.max_provider_requests_per_stage, 15)

    def test_provider_max_resolution_uses_capability_helper(self) -> None:
        profile = type(
            "Profile",
            (),
            {
                "id": "gemini-user-a",
                "provider": "gemini",
                "model": "gemini/gemini-3.6-flash",
                "secret_env": "GEMINI_USER_A_KEY",
            },
        )()
        policy = ExecutionModePolicy(name="relaxed-acceptance", output_tokens="provider_max")
        with patch("llm.execution.resolve_provider_max_output_tokens", return_value=4096):
            self.assertEqual(resolve_output_tokens(profile, 256, policy), 4096)


if __name__ == "__main__":
    unittest.main()
