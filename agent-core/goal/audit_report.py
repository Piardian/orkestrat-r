from __future__ import annotations

from pathlib import Path
from typing import Any

from .audit_synthesizer import AuditSynthesizer


def _turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def _has_explicit_edit_intent(lowered: str) -> bool:
    edit_indicators = [
        "fix ",
        "fix the ",
        "repair ",
        "implement ",
        "apply a patch",
        "apply patch",
        "update ",
        "düzelt",
        "güncelle",
        "uygula",
        "implement et",
    ]
    if any(indicator in lowered for indicator in edit_indicators):
        return True

    english_scoped_edit = "only" in lowered and any(verb in lowered for verb in ("modify", "edit", "change"))
    turkish_scoped_edit = any(scope in lowered for scope in ("yalnızca", "sadece")) and any(
        verb in lowered for verb in ("değiştir", "düzenle", "güncelle")
    )
    return english_scoped_edit or turkish_scoped_edit


def is_read_only_audit(goal_text: str, plan_dict: dict[str, Any] | None = None) -> bool:
    if plan_dict and plan_dict.get("patch_expected") is False:
        return True

    lowered = _turkish_lower(goal_text or "")

    # These phrases explicitly forbid any edit, so they stay read-only even if the
    # prompt discusses a possible fix or patch as part of the analysis.
    hard_read_only_indicators = [
        "hiçbir dosyayı değiştirmeden",
        "değiştirmeden",
        "salt inceleme",
        "read-only",
        "read only",
        "no modification",
        "without modifying",
    ]
    if any(indicator in lowered for indicator in hard_read_only_indicators):
        return True

    # Scope restrictions such as "only modify X; do not modify Y" are edit tasks,
    # not audits. Explicit edit intent must therefore win over softer audit words.
    if _has_explicit_edit_intent(lowered):
        return False

    audit_indicators = [
        "audit",
        "incele",
        "rapor",
        "değiştirme",
        "teknik audit",
        "mimarisini belirle",
        "kod içinde olası bug",
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
        repo_path=repo_path,
        plan_dict=plan_dict,
        evidence_packet=evidence_packet,
        analyst_results=analyst_results,
        review_dict=review_dict,
    )
