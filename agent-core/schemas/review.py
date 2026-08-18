from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_FINAL_VERDICTS = {"PASS", "FAIL", "UNKNOWN"}
VALID_AGREEMENTS = {"FULL", "PARTIAL", "CONFLICT"}


@dataclass(frozen=True)
class Review:
    final_verdict: str
    confidence: float
    agreement: str
    reason: str
    analyst_a: dict[str, Any]
    analyst_b: dict[str, Any] | None
    analysts: list[dict[str, Any]]
    evidence: list[dict[str, str]]
    patch_required: bool = False
    risk_flags: list[str] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Review":
        final_verdict = str(raw.get("final_verdict", "UNKNOWN")).upper()
        if final_verdict not in VALID_FINAL_VERDICTS:
            final_verdict = "UNKNOWN"
        agreement = str(raw.get("agreement", "PARTIAL")).upper()
        if agreement not in VALID_AGREEMENTS:
            agreement = "PARTIAL"
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = raw.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        analysts = raw.get("analysts")
        if not isinstance(analysts, list):
            analysts = [
                item
                for item in (raw.get("analyst_a"), raw.get("analyst_b"))
                if isinstance(item, dict)
            ]
        return cls(
            final_verdict=final_verdict,
            confidence=max(0.0, min(1.0, confidence)),
            agreement=agreement,
            reason=str(raw.get("reason", "")),
            analyst_a=raw.get("analyst_a") if isinstance(raw.get("analyst_a"), dict) else {},
            analyst_b=raw.get("analyst_b") if isinstance(raw.get("analyst_b"), dict) else None,
            analysts=[item for item in analysts if isinstance(item, dict)],
            evidence=[
                {"path": str(item.get("path", "")), "lines": str(item.get("lines", ""))}
                for item in evidence[:10]
                if isinstance(item, dict)
            ],
            patch_required=bool(raw.get("patch_required", final_verdict == "FAIL")),
            risk_flags=[str(item) for item in raw.get("risk_flags", [])[:10]] if isinstance(raw.get("risk_flags"), list) else [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_verdict": self.final_verdict,
            "confidence": self.confidence,
            "agreement": self.agreement,
            "reason": self.reason,
            "analyst_a": self.analyst_a,
            "analyst_b": self.analyst_b,
            "analysts": self.analysts,
            "evidence": self.evidence,
            "patch_required": self.patch_required,
            "risk_flags": self.risk_flags or [],
        }
