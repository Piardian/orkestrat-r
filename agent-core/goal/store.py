from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from .model import GoalRecord
from .plan import GoalPlan
from .complexity import ComplexityAssessment
from .review import GoalReview


class GoalStore:
    def __init__(self, base_dir: str | Path = "runtime/goals") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, goal_id: str) -> Path:
        return self.goal_dir(goal_id) / "goal.json"

    def goal_dir(self, goal_id: str) -> Path:
        return self.base_dir / goal_id

    def legacy_path_for(self, goal_id: str) -> Path:
        return self.base_dir / f"{goal_id}.json"

    def save(self, record: GoalRecord) -> Path:
        target = self.path_for(record.goal_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile("w", delete=False, dir=self.base_dir, suffix=".tmp", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def save_plan(self, goal_id: str, filename: str, payload: dict[str, Any]) -> Path:
        target = self.goal_dir(goal_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def save_text(self, goal_id: str, filename: str, content: str) -> Path:
        target = self.goal_dir(goal_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def append_jsonl(self, goal_id: str, filename: str, payload: dict[str, Any]) -> Path:
        target = self.goal_dir(goal_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=target.parent, suffix=".tmp", encoding="utf-8") as tmp:
            if target.exists():
                tmp.write(target.read_text(encoding="utf-8"))
            tmp.write(line + "\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return target

    def read_jsonl(self, goal_id: str, filename: str) -> list[dict[str, Any]]:
        path = self.goal_dir(goal_id) / filename
        if not path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if isinstance(raw, dict):
                items.append(raw)
        return items

    def load(self, goal_id: str) -> GoalRecord:
        path = self.path_for(goal_id)
        if not path.exists():
            legacy = self.legacy_path_for(goal_id)
            if legacy.exists():
                path = legacy
        data = json.loads(path.read_text(encoding="utf-8"))
        return GoalRecord(
            goal_id=str(data["goal_id"]),
            goal=str(data["goal"]),
            repo=str(data["repo"]),
            status=str(data["status"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            phase=str(data.get("phase", "intake")),
            utc_timestamp=bool(data.get("utc_timestamp", True)),
            goal_type=str(data.get("goal_type", "CODE_MODIFICATION")),
            notes=[str(item) for item in data.get("notes", [])] if isinstance(data.get("notes"), list) else [],
        )

    def list_goal_ids(self) -> list[str]:
        ids = {path.stem for path in self.base_dir.glob("GOAL-*.json")}
        ids.update(path.name for path in self.base_dir.iterdir() if path.is_dir() and path.name.startswith("GOAL-"))
        return sorted(ids)

    def save_plan_bundle(self, record: GoalRecord, search_plan: dict[str, Any], evidence: dict[str, Any], plan: GoalPlan) -> dict[str, Path]:
        return {
            "goal": self.save(record),
            "search_plan": self.save_plan(record.goal_id, "search_plan.json", search_plan),
            "evidence": self.save_plan(record.goal_id, "evidence.json", evidence),
            "plan": self.save_plan(record.goal_id, "plan.json", plan.to_dict()),
        }

    def save_review_bundle(self, record: GoalRecord, analyst_reviews: list[dict[str, Any]], review: GoalReview) -> dict[str, Path]:
        saved: dict[str, Path] = {
            "goal": self.save(record),
            "review": self.save_plan(record.goal_id, "review.json", review.to_dict()),
        }
        for index, item in enumerate(analyst_reviews, start=1):
            saved[f"analyst_{index}"] = self.save_plan(record.goal_id, f"analyst-{index}-review.json", item)
        return saved

    def save_complexity_bundle(self, record: GoalRecord, complexity: ComplexityAssessment) -> dict[str, Path]:
        return {
            "goal": self.save(record),
            "complexity": self.save_plan(record.goal_id, "complexity.json", complexity.to_dict()),
        }
