from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from llm.client import GeminiClient, LLMError, OpenAICompatibleClient


@dataclass
class _FakeResponse:
    payload: dict

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str = "{}") -> None:
        super().__init__("https://example.test", code, "err", hdrs=None, fp=BytesIO(body.encode("utf-8")))
        self._body = body

    def read(self) -> bytes:  # type: ignore[override]
        return self._body.encode("utf-8")


class LLMClientRetryTests(unittest.TestCase):
    def test_gemini_timeout_then_success(self) -> None:
        calls: list[object] = []

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            calls.append(timeout)
            if len(calls) == 1:
                raise TimeoutError("timed out")
            return _FakeResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                }
            )

        with patch("llm.client.urllib.request.urlopen", side_effect=fake_urlopen), patch("llm.client.time.sleep") as sleep_mock:
            client = GeminiClient("gemini-3.5-flash-lite", "key", max_retries=2)
            response = client.generate("sys", "user", timeout=5)

        self.assertEqual(response.text, '{"ok": true}')
        self.assertEqual(response.retry_count, 1)
        self.assertEqual(len(calls), 2)
        sleep_mock.assert_called_once()

    def test_openai_compatible_timeout_bound_failure(self) -> None:
        calls: list[object] = []

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            calls.append(timeout)
            raise socket.timeout("timed out")

        import socket

        with patch("llm.client.urllib.request.urlopen", side_effect=fake_urlopen), patch("llm.client.time.sleep") as sleep_mock:
            client = OpenAICompatibleClient("model", "key", "https://example.test", max_retries=2)
            with self.assertRaises(LLMError) as ctx:
                client.generate("sys", "user", timeout=5)

        self.assertEqual(ctx.exception.kind, "CONNECTION_ERROR")
        self.assertEqual(ctx.exception.retry_count, 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_401_does_not_retry(self) -> None:
        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            raise _FakeHTTPError(401, '{"error":{"message":"unauthorized"}}')

        with patch("llm.client.urllib.request.urlopen", side_effect=fake_urlopen), patch("llm.client.time.sleep") as sleep_mock:
            client = GeminiClient("gemini-3.5-flash-lite", "key", max_retries=2)
            with self.assertRaises(LLMError) as ctx:
                client.generate("sys", "user", timeout=5)

        self.assertEqual(ctx.exception.kind, "AUTH_ERROR")
        self.assertEqual(ctx.exception.retry_count, 0)
        sleep_mock.assert_not_called()

    def test_429_quota_exhausted_does_not_retry(self) -> None:
        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            raise _FakeHTTPError(429, '{"error":{"message":"You exceeded your current quota, please check your plan and billing details."}}')

        with patch("llm.client.urllib.request.urlopen", side_effect=fake_urlopen), patch("llm.client.time.sleep") as sleep_mock:
            client = GeminiClient("gemini-3.5-flash-lite", "key", max_retries=5)
            with self.assertRaises(LLMError) as ctx:
                client.generate("sys", "user", timeout=5)

        self.assertEqual(ctx.exception.kind, "QUOTA_EXHAUSTED")
        self.assertEqual(ctx.exception.retry_count, 0)
        sleep_mock.assert_not_called()

    def test_503_retries_until_success(self) -> None:
        calls: list[int] = []

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            calls.append(1)
            if len(calls) < 3:
                raise _FakeHTTPError(503, '{"error":{"message":"temporarily unavailable"}}')
            return _FakeResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                }
            )

        with patch("llm.client.urllib.request.urlopen", side_effect=fake_urlopen), patch("llm.client.time.sleep") as sleep_mock:
            client = GeminiClient("gemini-3.5-flash-lite", "key", max_retries=5)
            response = client.generate("sys", "user", timeout=5)

        self.assertEqual(response.text, '{"ok": true}')
        self.assertEqual(response.retry_count, 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
