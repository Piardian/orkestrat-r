from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_VERDICTS = {"PASS", "FAIL", "UNKNOWN"}


@dataclass(frozen=True)
class Verdict:
    verdict: str
    confidence: float
    reason: str
    evidence: list[dict[str, str]]
    analyst: str | None = None
    profile: str | None = None
    uncertainties: list[str] | None = None
    risk_flags: list[str] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Verdict":
        verdict = str(raw.get("verdict", "UNKNOWN")).upper()
        if verdict not in VALID_VERDICTS:
            verdict = "UNKNOWN"
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        evidence = raw.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        safe_evidence = []
        for item in evidence[:10]:
            if isinstance(item, dict):
                safe_evidence.append({
                    "path": str(item.get("path", "")),
                    "lines": str(item.get("lines", "")),
                })
        uncertainties = raw.get("uncertainties", [])
        if not isinstance(uncertainties, list):
            uncertainties = []
        return cls(
            verdict=verdict,
            confidence=confidence,
            reason=str(raw.get("reason", "")),
            evidence=safe_evidence,
            analyst=str(raw.get("analyst")) if raw.get("analyst") else None,
            profile=str(raw.get("profile")) if raw.get("profile") else None,
            uncertainties=[str(item) for item in uncertainties[:10]],
            risk_flags=[str(item) for item in raw.get("risk_flags", [])[:10]] if isinstance(raw.get("risk_flags"), list) else [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence": self.evidence,
            "analyst": self.analyst,
            "profile": self.profile,
            "uncertainties": self.uncertainties or [],
            "risk_flags": self.risk_flags or [],
        }
