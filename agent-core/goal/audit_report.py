from __future__ import annotations

from pathlib import Path
from typing import Any

from .audit_synthesizer import AuditSynthesizer


def _turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def is_read_only_audit(goal_text: str, plan_dict: dict[str, Any] | None = None) -> bool:
    if plan_dict and plan_dict.get("patch_expected") is False:
        return True
    lowered = _turkish_lower(goal_text or "")
    audit_indicators = [
        "audit",
        "incele",
        "rapor",
        "hiçbir dosyayı değiştirmeden",
        "değiştirmeden",
        "değiştirme",
        "salt inceleme",
        "teknik audit",
        "mimarisini belirle",
        "kod içinde olası bug",
        "read-only",
        "read only",
        "no modification",
        "do not modify",
    ]
    return any(indicator in lowered for indicator in audit_indicators)


def generate_audit_report(
    goal_id: str,
    goal_text: str,
    repo_path: str | Path,
    plan_dict: dict[str, Any],
    evidence_packet: dict[str, Any],
    analyst_results: list[Any],
    review_dict: dict[str, Any],
) -> str:
    synthesizer = AuditSynthesizer(repo_path)
    return synthesizer.synthesize(
        goal_id=goal_id,
        goal_text=goal_text,
        plan_dict=plan_dict,
        evidence_packet=evidence_packet,
        analyst_results=analyst_results,
        review_dict=review_dict,
    )
