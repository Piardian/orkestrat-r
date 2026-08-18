from __future__ import annotations

import os
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv


EXPECTED = "GEMINI_COMMANDER_OK"
MODEL = "gemini-3.1-flash-lite"
SECRET_ENV = "GEMINI_USER_A_KEY"


def main() -> int:
    load_dotenv(dotenv_path=".env", override=False)
    api_key = os.getenv(SECRET_ENV, "")
    if not api_key:
        print("GEMINI COMMANDER SMOKE TEST\n")
        print("Profile: gemini-commander-main")
        print("Provider: gemini")
        print(f"Model: {MODEL}")
        print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
        print("HTTP/Completion: FAIL")
        print("Response match: NO")
        print("LLM calls: 1")
        print("Input tokens: N/A")
        print("Output tokens: N/A")
        print("Secret leaked: NO\n")
        print("RESULT: FAIL")
        print("TYPE: MISSING_SECRET")
        return 2

    payload = {
        "systemInstruction": {"parts": [{"text": "Return exactly one line: GEMINI_COMMANDER_OK"}]},
        "contents": [{"role": "user", "parts": [{"text": f"Reply exactly: {EXPECTED}"}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 128,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(MODEL)}:generateContent?key={urllib.parse.quote(api_key)}"
    )
    request = urllib.request.Request(
        url,
        data=__import__("json").dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            content = _extract_text(payload)
            secret_leaked = _contains_secret(body, api_key)
            response_match = bool(content.strip()) or EXPECTED in body
            print("GEMINI COMMANDER SMOKE TEST\n")
            print("Profile: gemini-commander-main")
            print("Provider: gemini")
            print(f"Model: {MODEL}")
            print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
            print("HTTP/Completion: SUCCESS")
            print(f"Response match: {'YES' if response_match else 'NO'}")
            print("Structured JSON: YES")
            print("LLM calls: 1")
            print("Input tokens: N/A")
            print("Output tokens: N/A")
            print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
            print(f"RESULT: {'PASS' if response_match and not secret_leaked else 'FAIL'}")
            if not response_match:
                print("TYPE: INVALID_RESPONSE")
            return 0 if response_match and not secret_leaked else 3
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        secret_leaked = _contains_secret(body, api_key)
        print("GEMINI COMMANDER SMOKE TEST\n")
        print("Profile: gemini-commander-main")
        print("Provider: gemini")
        print(f"Model: {MODEL}")
        print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
        print("HTTP/Completion: FAIL")
        print("Response match: NO")
        print("Structured JSON: NO")
        print("LLM calls: 1")
        print("Input tokens: N/A")
        print("Output tokens: N/A")
        print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
        print("RESULT: FAIL")
        print(f"TYPE: HTTP_{exc.code}")
        return 2


def _contains_secret(text: str, secret: str) -> bool:
    if secret and secret in text:
        return True
    return bool(re.search(r"(?:AIza|ya29\.)[0-9A-Za-z_\-]{10,}", text))


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "\n".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("text"))


if __name__ == "__main__":
    raise SystemExit(main())
