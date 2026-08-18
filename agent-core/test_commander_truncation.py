from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from commander import Commander
from llm.client import BaseLLMClient, LLMError, LLMResponse


class FakeCommanderClient(BaseLLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0
        self.options: list[dict] = []

    def generate(self, system_prompt: str, user_prompt: str, **options):  # noqa: ANN001
        self.calls += 1
        self.options.append(dict(options))
        index = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[index]


class CommanderTruncationTests(unittest.TestCase):
    def test_truncated_then_regeneration_success(self) -> None:
        client = FakeCommanderClient(
            [
                LLMResponse(text='{"task":"search"', output_tokens=5, finish_reason="MAX_TOKENS"),
                LLMResponse(
                    text=json.dumps(
                        {
                            "task": "search",
                            "search_terms": ["calculator.py", "add"],
                            "max_files": 3,
                            "max_lines_per_file": 40,
                        }
                    ),
                    output_tokens=7,
                    finish_reason="STOP",
                ),
            ]
        )
        plan, usage = Commander(client).create_plan("Add type hints", "repo")

        self.assertEqual(plan.search_terms, ["calculator.py", "add"])
        self.assertEqual(usage["truncation_regeneration"], 1)
        self.assertEqual(usage["provider_requests"], 2)
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.options[0]["max_tokens"], 256)
        self.assertEqual(client.options[1]["max_tokens"], 512)

    def test_truncated_twice_is_bounded_failure(self) -> None:
        client = FakeCommanderClient(
            [
                LLMResponse(text='{"task":"search"', output_tokens=5, finish_reason="MAX_TOKENS"),
                LLMResponse(text='{"task":"search"', output_tokens=5, finish_reason="MAX_TOKENS"),
            ]
        )
        with self.assertRaises(LLMError) as ctx:
            Commander(client).create_plan("Add type hints", "repo")
        self.assertEqual(ctx.exception.kind, "TRUNCATED_MODEL_RESPONSE")
        self.assertEqual(client.calls, 2)

    def test_malformed_json_uses_existing_repair(self) -> None:
        client = FakeCommanderClient(
            [
                LLMResponse(text='{"task":"search","search_terms":["a"],}', output_tokens=5, finish_reason="STOP"),
                LLMResponse(
                    text=json.dumps(
                        {
                            "task": "search",
                            "search_terms": ["calculator.py"],
                            "max_files": 3,
                            "max_lines_per_file": 40,
                        }
                    ),
                    output_tokens=6,
                    finish_reason="STOP",
                ),
            ]
        )
        plan, usage = Commander(client).create_plan("Add type hints", "repo")
        self.assertEqual(plan.search_terms, ["calculator.py"])
        self.assertEqual(usage["truncation_regeneration"], 0)
        self.assertEqual(client.calls, 2)

    def test_stop_with_valid_json_passes(self) -> None:
        client = FakeCommanderClient(
            [
                LLMResponse(
                    text=json.dumps(
                        {
                            "task": "search",
                            "search_terms": ["calculator.py"],
                            "max_files": 3,
                            "max_lines_per_file": 40,
                        }
                    ),
                    output_tokens=7,
                    finish_reason="STOP",
                )
            ]
        )
        plan, usage = Commander(client).create_plan("Add type hints", "repo")
        self.assertEqual(plan.search_terms, ["calculator.py"])
        self.assertEqual(usage["truncation_regeneration"], 0)
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
