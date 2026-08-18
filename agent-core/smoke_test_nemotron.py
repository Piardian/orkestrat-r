from __future__ import annotations

from pathlib import Path
import re

from dotenv import load_dotenv

from llm.client import LLMError, OpenAICompatibleClient
from registry import ProfileRegistry


EXPECTED = "NEMOTRON_OK"
PROFILE_ID = "nemotron-main"


def main() -> int:
    load_dotenv(dotenv_path=".env", override=False)
    registry = ProfileRegistry(Path("config") / "profiles.yaml")
    profile = registry.get(PROFILE_ID)
    secret = _secret(profile.secret_env)
    if not secret:
        _print_failure(profile, "MISSING_SECRET", "N/A", "N/A", secret_leaked=False)
        return 2
    if profile.provider != "openai-compatible":
        _print_failure(profile, "UNKNOWN_PROVIDER_ERROR", "N/A", "N/A", secret_leaked=False)
        return 2
    if not profile.base_url:
        _print_failure(profile, "CONNECTION_ERROR", "N/A", "N/A", secret_leaked=False)
        return 2

    client = OpenAICompatibleClient(profile.model, secret, profile.base_url, max_retries=2)
    output = ""
    try:
        response = client.generate(
            "",
            f"Return exactly: {EXPECTED}",
            max_tokens=64,
            temperature=0.0,
            stream=False,
            chat_template_kwargs={"enable_thinking": False},
            timeout=60,
        )
        output = response.text.strip()
        response_match = output == EXPECTED
        secret_leaked = _contains_secret(output, secret)
        print("NEMOTRON SMOKE TEST\n")
        print(f"Profile: {profile.id}")
        print(f"Provider: {profile.provider}")
        print(f"Model: {profile.model}")
        print(f"Endpoint: {profile.base_url}")
        print("HTTP/Completion: SUCCESS")
        print(f"Response match: {'YES' if response_match else 'NO'}")
        print("LLM calls: 1")
        print(f"Input tokens: {_token_value(response.input_tokens)}")
        print(f"Output tokens: {_token_value(response.output_tokens)}")
        print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
        print(f"RESULT: {'PASS' if response_match and not secret_leaked else 'FAIL'}")
        if not response_match:
            print("TYPE: INVALID_RESPONSE")
        return 0 if response_match and not secret_leaked else 3
    except LLMError as exc:
        kind = _normalize_error_kind(exc.kind)
        secret_leaked = _contains_secret(str(exc), secret) or _contains_secret(output, secret)
        _print_failure(profile, kind, "N/A", "N/A", secret_leaked)
        return 2


def _secret(name: str | None) -> str:
    if not name:
        return ""
    import os

    return os.getenv(name, "")


def _token_value(value: int | None) -> str:
    return str(value) if value is not None else "N/A"


def _normalize_error_kind(kind: str) -> str:
    allowed = {
        "MISSING_SECRET",
        "AUTH_ERROR",
        "MODEL_NOT_FOUND",
        "RATE_LIMIT",
        "CONNECTION_ERROR",
        "INVALID_RESPONSE",
        "UNKNOWN_PROVIDER_ERROR",
    }
    if kind == "NETWORK_ERROR":
        return "CONNECTION_ERROR"
    if kind in allowed:
        return kind
    return "UNKNOWN_PROVIDER_ERROR"


def _print_failure(profile, kind: str, input_tokens: str, output_tokens: str, secret_leaked: bool) -> None:
    print("NEMOTRON SMOKE TEST\n")
    print(f"Profile: {profile.id}")
    print(f"Provider: {profile.provider}")
    print(f"Model: {profile.model}")
    print(f"Endpoint: {profile.base_url}")
    print("HTTP/Completion: FAIL")
    print("Response match: NO")
    print("LLM calls: 1")
    print(f"Input tokens: {input_tokens}")
    print(f"Output tokens: {output_tokens}")
    print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
    print("RESULT: FAIL")
    print(f"TYPE: {kind}")
    print(f"PROFILE: {profile.id}")
    print(f"MODEL: {profile.model}")


def _contains_secret(text: str, secret: str) -> bool:
    if secret and secret in text:
        return True
    return bool(re.search(r"nvapi-[0-9A-Za-z_\-]{20,}", text))


if __name__ == "__main__":
    raise SystemExit(main())
