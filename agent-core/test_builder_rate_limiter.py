from __future__ import annotations

from dataclasses import dataclass
import unittest
from unittest.mock import patch

from goal.builder_policy import BuilderPolicy
from goal.builder_rate_limiter import BuilderRateLimitConfig, BuilderRateLimiter
from goal.openhands_adapter import _RateLimitedLLM


@dataclass
class _FakeResponse:
    text: str = "ok"


class _FakeLLM:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def completion(self, *args, **kwargs):  # noqa: ANN001
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BuilderRateLimiterTests(unittest.TestCase):
    def test_reserve_waits_only_when_limit_reached(self) -> None:
        now = [0.0]
        sleeps: list[float] = []

        def clock() -> float:
            return now[0]

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        limiter = BuilderRateLimiter(
            BuilderRateLimitConfig(enabled=True, requests_per_minute=20, safety_margin=1, window_seconds=60),
            clock=clock,
            sleeper=sleeper,
        )
        scope = "scope"
        for _ in range(18):
            self.assertEqual(limiter.reserve(scope), 0.0)
        self.assertEqual(limiter.reserve(scope), 0.0)
        self.assertEqual(len(sleeps), 0)
        self.assertEqual(limiter.observe(scope, "profile", "provider", "model").request_count, 19)
        self.assertGreater(limiter.reserve(scope), 0.0)

    def test_reserve_waits_after_limit_and_expires_old_entries(self) -> None:
        now = [0.0]
        sleeps: list[float] = []

        def clock() -> float:
            return now[0]

        def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        limiter = BuilderRateLimiter(
            BuilderRateLimitConfig(enabled=True, requests_per_minute=3, safety_margin=0, window_seconds=60),
            clock=clock,
            sleeper=sleeper,
        )
        scope = "scope"
        self.assertEqual(limiter.reserve(scope), 0.0)
        self.assertEqual(limiter.reserve(scope), 0.0)
        self.assertEqual(limiter.reserve(scope), 0.0)
        self.assertGreater(limiter.reserve(scope), 0.0)
        self.assertTrue(sleeps)
        now[0] += 61.0
        self.assertEqual(limiter.reserve(scope), 0.0)

    def test_scope_id_is_secret_safe(self) -> None:
        limiter = BuilderRateLimiter()
        scope_a = limiter.scope_id(profile_id="builder", provider="gemini", model="gemini/flash", api_key="secret-a")
        scope_b = limiter.scope_id(profile_id="builder", provider="gemini", model="gemini/flash", api_key="secret-b")
        self.assertNotEqual(scope_a, scope_b)
        self.assertEqual(len(scope_a), 24)


class BuilderOpenHandsRetryTests(unittest.TestCase):
    def test_timeout_then_success_is_retried_once(self) -> None:
        limiter = BuilderRateLimiter(BuilderRateLimitConfig(enabled=False))
        fake_llm = _FakeLLM([TimeoutError("timed out"), _FakeResponse("ok")])
        wrapper = _RateLimitedLLM(
            llm=fake_llm,
            limiter=limiter,
            scope_id="scope",
            profile_id="builder",
            provider="gemini",
            model="gemini/flash",
            policy=BuilderPolicy(rate_limit_retry_attempts=3, rate_limit_backoff_seconds=[0.0, 0.0, 0.0]),
        )

        with patch("goal.openhands_adapter.time.sleep") as sleep_mock:
            response = wrapper.completion([])

        self.assertEqual(response.text, "ok")
        self.assertEqual(fake_llm.calls, 2)
        self.assertEqual(wrapper.runtime()["provider_retries"], 1)
        self.assertEqual(wrapper.runtime()["provider_timeout_count"], 1)
        sleep_mock.assert_called_once()

    def test_401_is_not_retried(self) -> None:
        limiter = BuilderRateLimiter(BuilderRateLimitConfig(enabled=False))
        fake_llm = _FakeLLM([RuntimeError("401 unauthorized")])
        wrapper = _RateLimitedLLM(
            llm=fake_llm,
            limiter=limiter,
            scope_id="scope",
            profile_id="builder",
            provider="gemini",
            model="gemini/flash",
            policy=BuilderPolicy(rate_limit_retry_attempts=3, rate_limit_backoff_seconds=[0.0, 0.0, 0.0]),
        )

        with self.assertRaises(RuntimeError):
            wrapper.completion([])

        self.assertEqual(fake_llm.calls, 1)
        self.assertEqual(wrapper.runtime()["provider_retries"], 0)


if __name__ == "__main__":
    unittest.main()
