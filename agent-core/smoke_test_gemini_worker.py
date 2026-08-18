from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv


EXPECTED = "WORKER_OK"
MODEL = "gemini-3.1-flash-lite"
PROFILE = "gemini-user-b"
SECRET_ENV = "GEMINI_USER_B_KEY"


def main() -> int:
    load_dotenv(dotenv_path=".env", override=False)
    api_key = os.getenv(SECRET_ENV, "")
    if not api_key:
        print("GEMINI WORKER SMOKE TEST\n")
        print(f"Profile: {PROFILE}")
        print("Provider: gemini")
        print(f"Model: {MODEL}")
        print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
        print("HTTP/Completion: FAIL")
        print("Response match: NO")
        print("Structured JSON: NO")
        print("Secret leaked: NO\n")
        print("RESULT: FAIL")
        print("TYPE: MISSING_SECRET")
        return 2

    payload = {
        "systemInstruction": {"parts": [{"text": "Return JSON only with fields result and model."}]},
        "contents": [{"role": "user", "parts": [{"text": f"Reply exactly: {EXPECTED}"}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 64,
            "responseMimeType": "application/json",
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(MODEL)}:generateContent?key={urllib.parse.quote(api_key)}"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
            secret_leaked = _contains_secret(body, api_key)
            structured_ok = EXPECTED in body and _looks_like_json(body)
            print("GEMINI WORKER SMOKE TEST\n")
            print(f"Profile: {PROFILE}")
            print("Provider: gemini")
            print(f"Model: {MODEL}")
            print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
            print("HTTP/Completion: SUCCESS")
            print(f"Response match: {'YES' if EXPECTED in body else 'NO'}")
            print(f"Structured JSON: {'YES' if structured_ok else 'NO'}")
            print("LLM calls: 1")
            print("Input tokens: N/A")
            print("Output tokens: N/A")
            print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
            print(f"RESULT: {'PASS' if structured_ok and not secret_leaked else 'FAIL'}")
            if not structured_ok:
                print("TYPE: INVALID_RESPONSE")
            return 0 if structured_ok and not secret_leaked else 3
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        secret_leaked = _contains_secret(body, api_key)
        print("GEMINI WORKER SMOKE TEST\n")
        print(f"Profile: {PROFILE}")
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


def _looks_like_json(text: str) -> bool:
    text = text.strip()
    return text.startswith("{") and text.endswith("}")


if __name__ == "__main__":
    raise SystemExit(main())
