from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SearchPlan:
    task: str
    search_terms: list[str]
    max_search_results: int = 20
    max_files: int = 5
    max_lines_per_file: int = 80
    max_chars_per_file: int = 12000
    max_command_output_lines: int = 50
    max_test_output_lines: int = 50
    finish_reason: str | None = None
    truncation_regeneration: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any], fallback_task: str = "") -> "SearchPlan":
        terms = [str(item).strip() for item in raw.get("search_terms", []) if str(item).strip()]
        return cls(
            task=str(raw.get("task") or fallback_task),
            search_terms=terms[:5],
            max_search_results=_clamp(raw.get("max_search_results", 20), 1, 20),
            max_files=_clamp(raw.get("max_files", 5), 1, 5),
            max_lines_per_file=_clamp(raw.get("max_lines_per_file", 80), 1, 100),
            max_chars_per_file=_clamp(raw.get("max_chars_per_file", 12000), 1000, 12000),
            max_command_output_lines=_clamp(raw.get("max_command_output_lines", 50), 1, 50),
            max_test_output_lines=_clamp(raw.get("max_test_output_lines", 50), 1, 50),
            finish_reason=str(raw.get("finish_reason")) if raw.get("finish_reason") is not None else None,
            truncation_regeneration=int(raw.get("truncation_regeneration", 0) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "search_terms": self.search_terms,
            "max_search_results": self.max_search_results,
            "max_files": self.max_files,
            "max_lines_per_file": self.max_lines_per_file,
            "max_chars_per_file": self.max_chars_per_file,
            "max_command_output_lines": self.max_command_output_lines,
            "max_test_output_lines": self.max_test_output_lines,
            "finish_reason": self.finish_reason,
            "truncation_regeneration": self.truncation_regeneration,
        }


def _clamp(value: Any, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = maximum
    return max(minimum, min(maximum, number))
