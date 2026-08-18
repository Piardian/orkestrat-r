from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv


MODEL = "gemini-3.1-flash-lite"
PROFILE = "gemini-commander-main"
SECRET_ENV = "GEMINI_USER_A_KEY"
EXPECTED_PLAN_VERSION = 1


def main() -> int:
    load_dotenv(dotenv_path=".env", override=False)
    api_key = os.getenv(SECRET_ENV, "")
    if not api_key:
        _print_failure("MISSING_SECRET", secret_leaked=False)
        return 2

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": "Return JSON only with plan_version, goal_id, objective, summary, tasks, candidate_files, acceptance_criteria, verification, risks, constraints, patch_expected, uncertainties, evidence_refs.",
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": "Goal: planner smoke for commander routing. Return a concise structured plan with plan_version 1 and candidate_files including smoke_test_gemini_planner.py.",
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 256,
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
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            text = _extract_text(data)
            secret_leaked = _contains_secret(body, api_key)
            plan = _parse_plan(text)
            structured_ok = bool(text.strip().startswith("{") and "plan_version" in text)
            print("GEMINI PLANNER SMOKE TEST\n")
            print(f"Profile: {PROFILE}")
            print("Provider: gemini")
            print(f"Model: {MODEL}")
            print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
            print("HTTP/Completion: SUCCESS")
            print(f"Structured JSON: {'YES' if structured_ok else 'NO'}")
            print("LLM calls: 1")
            print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
            print(f"RESULT: {'PASS' if structured_ok and not secret_leaked else 'FAIL'}")
            if not structured_ok:
                print("TYPE: INVALID_RESPONSE")
                print(f"RAW_PREVIEW: {text[:300]!r}")
            return 0 if structured_ok and not secret_leaked else 3
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        secret_leaked = _contains_secret(body, api_key)
        print("GEMINI PLANNER SMOKE TEST\n")
        print(f"Profile: {PROFILE}")
        print("Provider: gemini")
        print(f"Model: {MODEL}")
        print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
        print("HTTP/Completion: FAIL")
        print("Structured JSON: NO")
        print("LLM calls: 1")
        print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
        print("RESULT: FAIL")
        print(f"TYPE: HTTP_{exc.code}")
        return 2


def _parse_plan(text: str) -> dict | None:
    cleaned = text.strip()
    cleaned = cleaned.strip("`").strip()
    if not cleaned:
        return None
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start : end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "\n".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("text"))


def _contains_secret(text: str, secret: str) -> bool:
    if secret and secret in text:
        return True
    return bool(re.search(r"(?:AIza|ya29\.)[0-9A-Za-z_\-]{10,}", text))


def _print_failure(kind: str, secret_leaked: bool) -> None:
    print("GEMINI PLANNER SMOKE TEST\n")
    print(f"Profile: {PROFILE}")
    print("Provider: gemini")
    print(f"Model: {MODEL}")
    print("Endpoint: https://generativelanguage.googleapis.com/v1beta")
    print("HTTP/Completion: FAIL")
    print("Structured JSON: NO")
    print("LLM calls: 1")
    print(f"Secret leaked: {'YES' if secret_leaked else 'NO'}\n")
    print("RESULT: FAIL")
    print(f"TYPE: {kind}")


if __name__ == "__main__":
    raise SystemExit(main())
