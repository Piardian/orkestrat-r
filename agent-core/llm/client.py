from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class LLMError(RuntimeError):
    def __init__(self, message: str, kind: str = "LLM_ERROR", retry_count: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_count = retry_count


@dataclass
class LLMResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    retry_count: int = 0
    finish_reason: str | None = None
    rate_limit_retry_count: int = 0
    service_unavailable_retry_count: int = 0


class BaseLLMClient:
    def generate(self, system_prompt: str, user_prompt: str, **options: Any) -> LLMResponse:
        raise NotImplementedError


class GeminiClient(BaseLLMClient):
    def __init__(self, model: str, api_key: str, max_retries: int = 2, retry_wait_cap_seconds: int = 60) -> None:
        self.model = model.removeprefix("gemini/")
        self.api_key = api_key
        self.max_retries = max(0, int(max_retries))
        self.retry_wait_cap_seconds = max(1, int(retry_wait_cap_seconds))

    def generate(self, system_prompt: str, user_prompt: str, **options: Any) -> LLMResponse:
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": float(options.get("temperature", 0.1)),
                "maxOutputTokens": int(options.get("max_tokens", 1200)),
                "responseMimeType": "application/json",
            },
        }
        encoded = json.dumps(payload).encode("utf-8")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(self.model)}:generateContent?key={urllib.parse.quote(self.api_key)}"
        )

        retry_count = 0
        rate_limit_retry_count = 0
        service_unavailable_retry_count = 0
        while True:
            request = urllib.request.Request(
                url,
                data=encoded,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=int(options.get("timeout", 90))) as response:
                    data = json.loads(response.read().decode("utf-8"))
                text = _extract_text(data)
                usage = data.get("usageMetadata", {})
                return LLMResponse(
                    text=text,
                    input_tokens=usage.get("promptTokenCount"),
                    output_tokens=usage.get("candidatesTokenCount"),
                    retry_count=retry_count,
                    finish_reason=_extract_gemini_finish_reason(data),
                    rate_limit_retry_count=rate_limit_retry_count,
                    service_unavailable_retry_count=service_unavailable_retry_count,
                )
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if _is_retriable_http_status(exc.code) and retry_count < self.max_retries:
                    if exc.code == 429 and _is_quota_exhausted_body(body.lower()):
                        kind = _http_error_kind(exc.code, body)
                        raise LLMError(_safe_error(exc.code, body), kind=kind, retry_count=retry_count) from exc
                    retry_count += 1
                    if exc.code == 429:
                        rate_limit_retry_count += 1
                    if exc.code == 503:
                        service_unavailable_retry_count += 1
                    time.sleep(_retry_delay(body, exc.headers, retry_count, self.retry_wait_cap_seconds))
                    continue
                kind = _http_error_kind(exc.code, body)
                raise LLMError(_safe_error(exc.code, body), kind=kind, retry_count=retry_count) from exc
            except urllib.error.URLError as exc:
                if _is_retriable_url_error(exc) and retry_count < self.max_retries:
                    retry_count += 1
                    time.sleep(_retry_delay(str(exc.reason), None, retry_count, self.retry_wait_cap_seconds))
                    continue
                raise LLMError(f"Network error: {exc.reason}", kind="NETWORK_ERROR", retry_count=retry_count) from exc
            except (TimeoutError, socket.timeout) as exc:
                if retry_count < self.max_retries:
                    retry_count += 1
                    time.sleep(_retry_delay(str(exc), None, retry_count, self.retry_wait_cap_seconds))
                    continue
                raise LLMError("Network timeout", kind="CONNECTION_ERROR", retry_count=retry_count) from exc


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(self, model: str, api_key: str, base_url: str, max_retries: int = 2, retry_wait_cap_seconds: int = 60) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.retry_wait_cap_seconds = max(1, int(retry_wait_cap_seconds))

    def generate(self, system_prompt: str, user_prompt: str, **options: Any) -> LLMResponse:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(options.get("temperature", 0.1)),
            "max_tokens": int(options.get("max_tokens", 1200)),
            "stream": bool(options.get("stream", False)),
        }
        if "top_p" in options:
            payload["top_p"] = float(options["top_p"])
        if "chat_template_kwargs" in options:
            payload["chat_template_kwargs"] = options["chat_template_kwargs"]
        if "reasoning_budget" in options:
            payload["reasoning_budget"] = int(options["reasoning_budget"])
        encoded = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}/chat/completions"

        retry_count = 0
        rate_limit_retry_count = 0
        service_unavailable_retry_count = 0
        while True:
            request = urllib.request.Request(
                url,
                data=encoded,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=int(options.get("timeout", 300))) as response:
                    data = json.loads(response.read().decode("utf-8"))
                usage = data.get("usage", {})
                return LLMResponse(
                    text=_extract_openai_text(data),
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    retry_count=retry_count,
                    finish_reason=_extract_openai_finish_reason(data),
                    rate_limit_retry_count=rate_limit_retry_count,
                    service_unavailable_retry_count=service_unavailable_retry_count,
                )
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                _debug_nvidia_http_error(exc.code, body)
                if _is_retriable_http_status(exc.code) and retry_count < self.max_retries:
                    if exc.code == 429 and _is_quota_exhausted_body(body.lower()):
                        kind = _http_error_kind(exc.code, body)
                        raise LLMError(_safe_error(exc.code, body), kind=kind, retry_count=retry_count) from exc
                    retry_count += 1
                    if exc.code == 429:
                        rate_limit_retry_count += 1
                    if exc.code == 503:
                        service_unavailable_retry_count += 1
                    time.sleep(_retry_delay(body, exc.headers, retry_count, self.retry_wait_cap_seconds))
                    continue
                kind = _http_error_kind(exc.code, body)
                raise LLMError(_safe_error(exc.code, body), kind=kind, retry_count=retry_count) from exc
            except urllib.error.URLError as exc:
                _debug_nvidia_url_error(exc)
                if _is_retriable_url_error(exc) and retry_count < self.max_retries:
                    retry_count += 1
                    time.sleep(_retry_delay(str(exc.reason), None, retry_count, self.retry_wait_cap_seconds))
                    continue
                raise LLMError(f"Network error: {exc.reason}", kind="NETWORK_ERROR", retry_count=retry_count) from exc
            except (TimeoutError, socket.timeout) as exc:
                print("NVIDIA TIMEOUT")
                if retry_count < self.max_retries:
                    retry_count += 1
                    time.sleep(_retry_delay(str(exc), None, retry_count, self.retry_wait_cap_seconds))
                    continue
                raise LLMError("Network timeout", kind="CONNECTION_ERROR", retry_count=retry_count) from exc


def _extract_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "\n".join(str(part.get("text", "")) for part in parts if part.get("text"))


def _extract_gemini_finish_reason(data: dict[str, Any]) -> str | None:
    candidates = data.get("candidates", [])
    if not candidates:
        return None
    reason = candidates[0].get("finishReason")
    return str(reason) if reason else None


def _extract_openai_text(data: dict[str, Any]) -> str:
    choices = data.get("choices", [])
    if not choices:
        return ""
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content)


def _extract_openai_finish_reason(data: dict[str, Any]) -> str | None:
    choices = data.get("choices", [])
    if not choices:
        return None
    reason = choices[0].get("finish_reason")
    return str(reason) if reason else None


def _retry_delay(body: str, headers: Any | None = None, retry_count: int = 1, cap_seconds: int = 60) -> float:
    if headers:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(cap_seconds), max(1.0, float(retry_after)))
            except ValueError:
                pass
    match = re.search(r'"retryDelay"\s*:\s*"(\d+)s"', body)
    if match:
        return min(float(cap_seconds), max(1.0, float(match.group(1))))
    if retry_count == 1:
        return 30.0
    if retry_count == 2:
        return 60.0
    return 10.0


def _http_error_kind(status: int, body: str) -> str:
    lowered = body.lower()
    if status in {429, 403} and _is_quota_exhausted_body(lowered):
        return "QUOTA_EXHAUSTED"
    if status == 401 or status == 403:
        return "AUTH_ERROR"
    if status == 404 or "model" in lowered and "not found" in lowered:
        return "MODEL_NOT_FOUND"
    if status == 429:
        return "RATE_LIMIT"
    if status == 503:
        return "SERVICE_UNAVAILABLE"
    if status == 408:
        return "CONNECTION_ERROR"
    if 500 <= status <= 599:
        return "UNKNOWN_PROVIDER_ERROR"
    return "HTTP_ERROR"


def _is_retriable_http_status(status: int) -> bool:
    return status in {429, 503, 408}


def _is_retriable_url_error(exc: urllib.error.URLError) -> bool:
    reason = exc.reason
    if isinstance(reason, TimeoutError):
        return True
    if isinstance(reason, socket.timeout):
        return True
    reason_text = str(reason).lower()
    return "timed out" in reason_text or "timeout" in reason_text or "temporarily unavailable" in reason_text


def _is_quota_exhausted_body(lowered_body: str) -> bool:
    if "quota exceeded" in lowered_body:
        return True
    if "daily quota" in lowered_body or "project quota" in lowered_body:
        return True
    if "check your plan and billing details" in lowered_body:
        return True
    if "billing" in lowered_body and "quota" in lowered_body:
        return True
    return False


def _safe_error(status: int, body: str) -> str:
    try:
        data = json.loads(body)
        message = data.get("error", {}).get("message", "")
    except json.JSONDecodeError:
        message = body[:300]
    message = re.sub(r"(key=)[^&\s]+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)(bearer\s+)[0-9a-z_\-\.]+", r"\1[REDACTED]", message)
    message = re.sub(r"nvapi-[0-9A-Za-z_\-]+", "[REDACTED]", message)
    return f"LLM request failed with HTTP {status}: {message}"


def _debug_nvidia_http_error(status: int, body: str) -> None:
    print(f"NVIDIA HTTP ERROR: {status}")
    print(_redact_debug_body(body)[:2000])


def _debug_nvidia_url_error(exc: urllib.error.URLError) -> None:
    reason = exc.reason
    print(f"NVIDIA URL ERROR: {type(reason).__name__}: {reason}")


def _redact_debug_body(body: str) -> str:
    redacted = re.sub(r"nvapi-[0-9A-Za-z_\-]+", "[REDACTED]", body)
    redacted = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED]", redacted)
    redacted = re.sub(r"sk-[0-9A-Za-z_\-]{20,}", "[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(authorization|api[_-]?key|secret|token)(\s*[=:]\s*)([^\s\"']+)", r"\1\2[REDACTED]", redacted)
    return redacted
