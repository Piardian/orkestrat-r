from __future__ import annotations

from typing import Any
import json
import re

from evidence.sanitizer import redact_text

from .client import BaseLLMClient, LLMError


REPAIR_SYSTEM_PROMPT = """Return the user's previous answer as valid JSON only.
No markdown.
No code fences.
No explanation.
Do not add new facts.
"""


TRUNCATED_FINISH_REASONS = {"MAX_TOKENS", "LENGTH", "length", "max_tokens"}


def parse_model_json(text: str | None, stage: str, finish_reason: str | None = None) -> dict[str, Any]:
    if text is None or not text.strip():
        _debug_raw(stage, text, finish_reason)
        raise LLMError("EMPTY_MODEL_RESPONSE", kind="EMPTY_MODEL_RESPONSE")

    cleaned = text.strip()
    if _is_truncated(cleaned, finish_reason):
        _debug_raw(stage, text, finish_reason)
        raise LLMError("TRUNCATED_MODEL_RESPONSE", kind="TRUNCATED_MODEL_RESPONSE")

    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    decode_error: json.JSONDecodeError | None = None
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError as exc:
        decode_error = exc

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    if _looks_like_truncated_json_error(cleaned, decode_error, finish_reason):
        _debug_raw(stage, text, finish_reason)
        raise LLMError("TRUNCATED_MODEL_RESPONSE", kind="TRUNCATED_MODEL_RESPONSE")

    _debug_raw(stage, text, finish_reason)
    raise LLMError("MALFORMED_MODEL_JSON", kind="MALFORMED_MODEL_JSON")


def parse_or_repair_json(
    client: BaseLLMClient,
    text: str | None,
    stage: str,
    usage: dict[str, int | None],
    finish_reason: str | None = None,
    max_repairs: int = 1,
    repair_max_tokens: int = 512,
    repair_timeout: int = 120,
) -> dict[str, Any]:
    max_repairs = max(0, int(max_repairs))
    try:
        return parse_model_json(text, stage, finish_reason)
    except LLMError as exc:
        if exc.kind == "TRUNCATED_MODEL_RESPONSE":
            raise
        if exc.kind not in {"MALFORMED_MODEL_JSON", "EMPTY_MODEL_RESPONSE"}:
            raise
        if max_repairs <= 0:
            raise
        current_text = text or ""
        current_finish_reason = finish_reason
        for repair_index in range(max_repairs):
            repair_response = client.generate(
                REPAIR_SYSTEM_PROMPT,
                current_text,
                max_tokens=repair_max_tokens,
                temperature=0.0,
                chat_template_kwargs={"enable_thinking": False},
                timeout=repair_timeout,
            )
            usage["provider_requests"] = int(usage.get("provider_requests") or 0) + 1 + int(repair_response.retry_count or 0)
            usage["input_tokens"] = _sum_tokens(usage.get("input_tokens"), repair_response.input_tokens)
            usage["output_tokens"] = _sum_tokens(usage.get("output_tokens"), repair_response.output_tokens)
            usage["retry_count"] = int(usage.get("retry_count") or 0) + int(repair_response.retry_count or 0)
            usage["rate_limit_retry_count"] = int(usage.get("rate_limit_retry_count") or 0) + int(repair_response.rate_limit_retry_count or 0)
            usage["service_unavailable_retry_count"] = int(usage.get("service_unavailable_retry_count") or 0) + int(repair_response.service_unavailable_retry_count or 0)
            usage["repair_count"] = int(usage.get("repair_count") or 0) + 1
            current_text = repair_response.text
            current_finish_reason = repair_response.finish_reason
            try:
                return parse_model_json(repair_response.text, f"{stage}-repair-{repair_index + 1}", repair_response.finish_reason)
            except LLMError as repair_exc:
                if repair_exc.kind == "TRUNCATED_MODEL_RESPONSE":
                    raise
                if repair_index + 1 >= max_repairs:
                    raise
                if repair_exc.kind not in {"MALFORMED_MODEL_JSON", "EMPTY_MODEL_RESPONSE"}:
                    raise
                continue
        raise LLMError("MALFORMED_MODEL_JSON", kind="MALFORMED_MODEL_JSON")


def _sum_tokens(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return int(left or 0) + int(right or 0)


def _debug_raw(stage: str, text: str | None, finish_reason: str | None = None) -> None:
    raw = text or ""
    print(f"{stage.upper()} RAW LENGTH: {len(raw)}")
    print(f"{stage.upper()} RAW PREVIEW: {redact_text(raw[:500])!r}")
    if finish_reason:
        print(f"{stage.upper()} FINISH REASON: {redact_text(str(finish_reason))!r}")


def _is_truncated(text: str, finish_reason: str | None) -> bool:
    if finish_reason and finish_reason in TRUNCATED_FINISH_REASONS:
        return True
    if "{" in text and "}" not in text:
        return True
    if text.count("{") > text.count("}"):
        return True
    return False


def _looks_like_truncated_json_error(text: str, exc: json.JSONDecodeError | None, finish_reason: str | None) -> bool:
    if not exc:
        return False
    if finish_reason and str(finish_reason).lower() not in {"", "unknown", "none"} and finish_reason not in TRUNCATED_FINISH_REASONS:
        return False
    message = exc.msg.lower()
    if "unterminated string" in message:
        return True
    if exc.pos >= max(0, len(text) - 3) and not text.rstrip().endswith("}") and message in {
        "expecting value",
        "expecting property name enclosed in double quotes",
        "expecting ',' delimiter",
        "expecting ':' delimiter",
    }:
        return True
    return False
