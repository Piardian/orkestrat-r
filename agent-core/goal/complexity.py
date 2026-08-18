from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any
import json
import re


DEFAULT_KEYWORD_GROUPS = {
    "security": ["auth", "authentication", "authorization", "security", "permission", "secret", "credential"],
    "trading": ["payment", "trading", "order", "execution"],
    "data": ["database", "migration", "schema"],
    "infra": ["infrastructure", "deployment", "dependency", "config"],
    "runtime": ["concurrency", "locking"],
    "interface": ["public api", "schema", "external integration", "config format", "dependency"],
}

DEFAULT_HARD_OVERRIDE_GROUPS = {
    "security": ["auth", "authentication", "authorization", "security", "permission", "secret", "credential"],
    "trading": ["trading", "order", "execution"],
    "destructive_db": ["database migration", "destructive migration", "schema migration"],
}


@dataclass(frozen=True)
class ComplexityPolicy:
    easy_max_score: int = 2
    medium_max_score: int = 5
    hard_max_score: int = 8
    keyword_groups: dict[str, list[str]] = field(default_factory=lambda: {key: list(value) for key, value in DEFAULT_KEYWORD_GROUPS.items()})
    hard_override_groups: dict[str, list[str]] = field(default_factory=lambda: {key: list(value) for key, value in DEFAULT_HARD_OVERRIDE_GROUPS.items()})

    @property
    def critical_min_score(self) -> int:
        return self.hard_max_score + 1


@dataclass(frozen=True)
class ComplexityFactor:
    name: str
    score: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComplexityAssessment:
    version: int
    goal_id: str
    score: int
    severity: str
    recommended_executor: str
    factors: list[ComplexityFactor]
    hard_overrides: list[str]
    candidate_file_count: int
    module_count: int
    review_risk_count: int
    llm_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["factors"] = [item.to_dict() for item in self.factors]
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ComplexityAssessment":
        return cls(
            version=int(raw.get("version", 1) or 1),
            goal_id=str(raw.get("goal_id", "")),
            score=int(raw.get("score", 0) or 0),
            severity=str(raw.get("severity", "EASY")).upper(),
            recommended_executor=str(raw.get("recommended_executor", "openhands")).lower(),
            factors=[ComplexityFactor(name=str(item.get("name", "")), score=int(item.get("score", 0) or 0), reason=str(item.get("reason", ""))) for item in raw.get("factors", []) if isinstance(item, dict)],
            hard_overrides=[str(item) for item in raw.get("hard_overrides", []) if str(item).strip()],
            candidate_file_count=int(raw.get("candidate_file_count", 0) or 0),
            module_count=int(raw.get("module_count", 0) or 0),
            review_risk_count=int(raw.get("review_risk_count", 0) or 0),
            llm_calls=int(raw.get("llm_calls", 0) or 0),
        )


class ComplexityAssessor:
    def __init__(self, policy: ComplexityPolicy | None = None) -> None:
        self.policy = policy or ComplexityPolicy()

    def assess(self, goal_dir: str | Path) -> ComplexityAssessment:
        goal_dir = Path(goal_dir)
        goal = self._load(goal_dir / "goal.json")
        plan = self._load(goal_dir / "plan.json")
        review = self._load(goal_dir / "review.json")
        analyst_reviews = self._load_analyst_reviews(goal_dir)

        candidate_files = [str(item) for item in plan.get("candidate_files", []) if str(item).strip()]
        tasks = [item for item in plan.get("tasks", []) if isinstance(item, dict)]
        verification = [str(item) for item in plan.get("verification", []) if str(item).strip()]
        acceptance_criteria = [str(item) for item in plan.get("acceptance_criteria", []) if str(item).strip()]
        uncertainties = [str(item) for item in plan.get("uncertainties", []) if str(item).strip()]
        risks = [str(item) for item in plan.get("risks", []) if str(item).strip()]
        review_risk_text = self._collect_review_text(review, analyst_reviews)

        factors: list[ComplexityFactor] = []
        score = 0
        hard_overrides: list[str] = []

        candidate_file_count = len(candidate_files)
        module_count = self._module_count(candidate_files)
        review_risk_count = self._review_risk_count(review, analyst_reviews)

        score, factors = self._add_scope_factors(score, factors, candidate_file_count, module_count, tasks, candidate_files)
        score, factors = self._add_quality_factors(score, factors, verification, acceptance_criteria, uncertainties, review_risk_count)
        score, factors, hard_overrides = self._apply_keyword_signals(score, factors, candidate_files, tasks, risks, uncertainties, review_risk_text, hard_overrides)

        severity = self._severity_from_score(score, hard_overrides, candidate_files, plan, review)
        recommended_executor = "openhands" if severity in {"EASY", "MEDIUM"} else "codex"
        return ComplexityAssessment(
            version=1,
            goal_id=str(goal.get("goal_id", "")),
            score=score,
            severity=severity,
            recommended_executor=recommended_executor,
            factors=factors,
            hard_overrides=hard_overrides,
            candidate_file_count=candidate_file_count,
            module_count=module_count,
            review_risk_count=review_risk_count,
            llm_calls=0,
        )

    def _add_scope_factors(self, score: int, factors: list[ComplexityFactor], candidate_file_count: int, module_count: int, tasks: list[dict[str, Any]], candidate_files: list[str]) -> tuple[int, list[ComplexityFactor]]:
        if candidate_file_count <= 2:
            factors.append(ComplexityFactor("candidate_files", 0, f"{candidate_file_count} candidate files"))
        elif candidate_file_count <= 4:
            score += 1
            factors.append(ComplexityFactor("candidate_files", 1, f"{candidate_file_count} candidate files"))
        else:
            score += 2
            factors.append(ComplexityFactor("candidate_files", 2, f"{candidate_file_count} candidate files"))

        if module_count >= 2:
            delta = 2 if module_count >= 3 else 1
            score += delta
            factors.append(ComplexityFactor("cross_module", delta, f"{module_count} top-level modules affected"))

        if len(tasks) >= 4:
            score += 1
            factors.append(ComplexityFactor("task_scope", 1, f"{len(tasks)} planned tasks"))
        if self._has_cross_module_refactor(candidate_files, tasks):
            score += 3
            factors.append(ComplexityFactor("cross_module_refactor", 3, "explicit cross-module refactor detected"))
        return score, factors

    def _add_quality_factors(self, score: int, factors: list[ComplexityFactor], verification: list[str], acceptance_criteria: list[str], uncertainties: list[str], review_risk_count: int) -> tuple[int, list[ComplexityFactor]]:
        if not verification:
            score += 2
            factors.append(ComplexityFactor("verification", 2, "verification is missing"))
        if not acceptance_criteria:
            score += 3
            factors.append(ComplexityFactor("acceptance_criteria", 3, "acceptance criteria are missing"))
        if uncertainties:
            delta = min(2, len(uncertainties))
            score += delta
            factors.append(ComplexityFactor("uncertainties", delta, f"{len(uncertainties)} uncertainties present"))
        if review_risk_count:
            delta = min(3, review_risk_count)
            score += delta
            factors.append(ComplexityFactor("review_risk", delta, f"{review_risk_count} reviewer risk signals"))
        return score, factors

    def _apply_keyword_signals(
        self,
        score: int,
        factors: list[ComplexityFactor],
        candidate_files: list[str],
        tasks: list[dict[str, Any]],
        risks: list[str],
        uncertainties: list[str],
        review_text: str,
        hard_overrides: list[str],
    ) -> tuple[int, list[ComplexityFactor], list[str]]:
        corpus = " ".join(
            [
                *candidate_files,
                *[str(task.get("title", "")) + " " + str(task.get("description", "")) for task in tasks],
                *risks,
                *uncertainties,
                review_text,
            ]
        ).lower()
        for group_name, keywords in self.policy.keyword_groups.items():
            hits = self._count_hits(corpus, keywords)
            if not hits:
                continue
            delta = 1 if hits == 1 else min(3, hits)
            score += delta
            factors.append(ComplexityFactor(group_name, delta, f"{hits} keyword hit(s) for {group_name}"))
        for override_name, keywords in self.policy.hard_override_groups.items():
            if self._contains_any(corpus, keywords):
                token = f"{override_name}: minimum HARD"
                if token not in hard_overrides:
                    hard_overrides.append(token)
                break
        return score, factors, hard_overrides

    def _severity_from_score(self, score: int, hard_overrides: list[str], candidate_files: list[str], plan: dict[str, Any], review: dict[str, Any]) -> str:
        severity = "EASY"
        if score >= 9:
            severity = "CRITICAL"
        elif score >= 6:
            severity = "HARD"
        elif score >= 3:
            severity = "MEDIUM"
        if hard_overrides:
            severity = "HARD" if severity in {"EASY", "MEDIUM"} else severity
        if self._has_strong_db_migration(candidate_files, plan, review):
            if severity in {"EASY", "MEDIUM"}:
                severity = "HARD"
            severity = "CRITICAL"
        return severity

    def _module_count(self, candidate_files: list[str]) -> int:
        modules = set()
        for file_path in candidate_files:
            parts = Path(file_path).parts
            if parts:
                modules.add(parts[0])
        return len(modules)

    def _review_risk_count(self, review: dict[str, Any], analyst_reviews: list[dict[str, Any]]) -> int:
        count = 0
        review_flags = review.get("risk_flags", [])
        if isinstance(review_flags, list):
            count += len([item for item in review_flags if str(item).strip()])
        for analyst in analyst_reviews:
            flags = analyst.get("risk_flags", [])
            if isinstance(flags, list):
                count += len([item for item in flags if str(item).strip()])
            uncertainties = analyst.get("uncertainties", [])
            if isinstance(uncertainties, list):
                count += len([item for item in uncertainties if str(item).strip()])
        return count

    def _collect_review_text(self, review: dict[str, Any], analyst_reviews: list[dict[str, Any]]) -> str:
        pieces = [str(review.get("reason", "")), " ".join(str(item) for item in review.get("risk_flags", []) if str(item).strip())]
        for analyst in analyst_reviews:
            pieces.append(str(analyst.get("reason", "")))
            pieces.append(" ".join(str(item) for item in analyst.get("risk_flags", []) if str(item).strip()))
            pieces.append(" ".join(str(item) for item in analyst.get("evidence", []) if str(item).strip()))
        return " ".join(piece for piece in pieces if piece).lower()

    def _has_cross_module_refactor(self, candidate_files: list[str], tasks: list[dict[str, Any]]) -> bool:
        refactor_terms = ("refactor", "rewrite", "migrate", "split", "move")
        if not self._contains_any(" ".join(candidate_files + [f"{t.get('title', '')} {t.get('description', '')}" for t in tasks]).lower(), refactor_terms):
            return False
        return self._module_count(candidate_files) >= 2 or len(candidate_files) >= 3

    def _has_strong_db_migration(self, candidate_files: list[str], plan: dict[str, Any], review: dict[str, Any]) -> bool:
        corpus = " ".join([*candidate_files, str(plan.get("summary", "")), str(review.get("reason", ""))]).lower()
        return "database migration" in corpus or "destructive migration" in corpus or "schema migration" in corpus

    def _count_hits(self, corpus: str, keywords: list[str]) -> int:
        return sum(1 for keyword in keywords if re.search(rf"(?<!\\w){re.escape(keyword.lower())}(?!\\w)", corpus))

    def _contains_any(self, corpus: str, keywords: list[str]) -> bool:
        return any(re.search(rf"(?<!\\w){re.escape(keyword.lower())}(?!\\w)", corpus) for keyword in keywords)

    def _load(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_analyst_reviews(self, goal_dir: Path) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        for path in sorted(goal_dir.glob("analyst-*-review.json")):
            reviews.append(self._load(path))
        return reviews
