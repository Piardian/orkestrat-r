from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
from typing import Any
import json
import re

from evidence.sanitizer import is_secret_path, redact_text
from handoff.builder import build_codex_handoff
from schemas import Review, Verdict

from .complexity import ComplexityAssessment
from .model import GoalRecord
from .plan import GoalPlan


MAX_PROMPT_BYTES = 24 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_PATCH_BYTES = 200 * 1024
MAX_STD_BYTES = 8 * 1024
MAX_MANUAL_ATTEMPTS = 3
DEFAULT_TERMINATOR = "::END_CODEX::"


@dataclass(frozen=True)
class CodexRequest:
    goal_id: str
    attempt_index: int
    goal: dict[str, Any]
    plan: dict[str, Any]
    review: dict[str, Any]
    complexity: dict[str, Any]
    evidence: dict[str, Any]
    allowed_files: list[str]
    forbidden_files: list[str]
    forbidden_areas: list[str]
    acceptance_criteria: list[str]
    verification_commands: list[str]
    constraints: list[str]
    complexity_reasons: list[str]
    severity: str
    recommended_executor: str
    source_repo: str
    target_repo: str
    runtime_workspace: str
    prompt_path: str
    handoff_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodexBuildArtifact:
    goal_id: str
    executor: str
    source: str
    changed_files: list[str]
    verification: str
    status: str
    failure_type: str | None
    patch_path: str | None
    patch_size: int
    verification_result: dict[str, Any] | None
    original_repo_modified: bool
    codex_response_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_codex_prompt(
    record: GoalRecord,
    plan: GoalPlan,
    review: Review,
    complexity: ComplexityAssessment,
    evidence_packet: dict[str, Any],
    runtime_goal_dir: Path,
    attempt_index: int,
) -> tuple[CodexRequest, str]:
    goal_dict = record.to_dict()
    plan_dict = plan.to_dict()
    review_dict = review.to_dict()
    complexity_dict = complexity.to_dict()
    allowed_files = [str(item) for item in plan.candidate_files if str(item).strip()]
    forbidden_files = _forbidden_files(allowed_files)
    forbidden_areas = _forbidden_areas(review, complexity)
    complexity_reasons = [f"{factor.name}: {factor.reason}" for factor in complexity.factors]

    handoff_path = runtime_goal_dir / "codex_handoff.md"
    build_codex_handoff(record.goal, Path(record.repo), review, [], evidence_packet, handoff_path)
    handoff_text = handoff_path.read_text(encoding="utf-8")

    prompt = _render_prompt(
        record.goal,
        plan_dict,
        evidence_packet,
        allowed_files,
        forbidden_files,
        forbidden_areas,
        plan.acceptance_criteria,
        plan.verification,
        plan.constraints,
        complexity,
        complexity_reasons,
        handoff_text,
    )
    prompt = redact_text(prompt)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        prompt = prompt.encode("utf-8")[:MAX_PROMPT_BYTES].decode("utf-8", errors="ignore")

    request = CodexRequest(
        goal_id=record.goal_id,
        attempt_index=attempt_index,
        goal=goal_dict,
        plan=plan_dict,
        review=review_dict,
        complexity=complexity_dict,
        evidence=evidence_packet,
        allowed_files=allowed_files,
        forbidden_files=forbidden_files,
        forbidden_areas=forbidden_areas,
        acceptance_criteria=[str(item) for item in plan.acceptance_criteria if str(item).strip()],
        verification_commands=[str(item) for item in plan.verification if str(item).strip()],
        constraints=[str(item) for item in plan.constraints if str(item).strip()],
        complexity_reasons=complexity_reasons,
        severity=complexity.severity,
        recommended_executor=complexity.recommended_executor,
        source_repo=record.repo,
        target_repo=record.repo,
        runtime_workspace=str(runtime_goal_dir.parent / "workspaces" / record.goal_id),
        prompt_path=str(runtime_goal_dir / "codex_prompt.md"),
        handoff_path=str(handoff_path),
    )
    return request, prompt


def extract_unified_diff(response_text: str) -> str:
    raw = _normalize_response_text(response_text)
    fenced = _extract_fenced_patch(raw)
    if fenced is not None:
        raw = fenced
    start = _find_patch_start(raw)
    if start < 0:
        raise ValueError("NO_PATCH_FOUND")
    patch = raw[start:].strip()
    if "```" in patch:
        patch = patch.split("```", 1)[0].strip()
    if not _looks_like_diff(patch):
        raise ValueError("NO_PATCH_FOUND")
    return patch + "\n"


def validate_patch_policy(patch_text: str, allowed_files: list[str]) -> list[str]:
    if len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES:
        raise ValueError("PATCH_TOO_LARGE")
    if _looks_like_binary_patch(patch_text):
        raise ValueError("BINARY_PATCH_REJECTED")
    if _contains_secret_material(patch_text):
        raise ValueError("SECRET_CONTENT_DETECTED")
    changed = _extract_changed_paths(patch_text)
    violations: list[str] = []
    allowed = {Path(item).as_posix().lstrip("./") for item in allowed_files}
    for item in changed:
        if not _is_safe_relative_path(item):
            violations.append(item)
            continue
        normalized = item.replace("\\", "/").lstrip("./")
        if normalized not in allowed:
            violations.append(item)
    return violations


def _render_prompt(
    goal: str,
    plan: dict[str, Any],
    evidence_packet: dict[str, Any],
    allowed_files: list[str],
    forbidden_files: list[str],
    forbidden_areas: list[str],
    acceptance_criteria: list[str],
    verification_commands: list[str],
    constraints: list[str],
    complexity: ComplexityAssessment,
    complexity_reasons: list[str],
    handoff_text: str,
) -> str:
    evidence_summary = _compact_evidence(evidence_packet)
    return f"""# Codex Manual Patch Request

## GOAL
{goal}

## PROBLEM
Goal state is CODEX_REQUIRED and the complexity gate selected Codex because the patch is HARD/CRITICAL.

## APPROVED PLAN
{json.dumps(plan, indent=2, ensure_ascii=False)}

## RELEVANT EVIDENCE
{json.dumps(evidence_summary, indent=2, ensure_ascii=False)}

## ALLOWED FILES
{_bullet_list(allowed_files)}

## FORBIDDEN FILES/AREAS
{_bullet_list(forbidden_files + forbidden_areas)}

## ACCEPTANCE CRITERIA
{_bullet_list(acceptance_criteria)}

## VERIFICATION COMMANDS
{_bullet_list(verification_commands)}

## CONSTRAINTS
{_bullet_list(constraints)}

## COMPLEXITY REASONS
Severity: {complexity.severity}
Recommended executor: {complexity.recommended_executor}
Reasons:
{_bullet_list(complexity_reasons)}

## COMPACT HANDOFF
{handoff_text}

Do not return the entire repository.
Do not include secrets.
Do not broaden scope.

Return:
1. Short implementation summary
2. Changed file list
3. One unified diff patch
4. Verification notes
"""


def _compact_evidence(evidence_packet: dict[str, Any]) -> dict[str, Any]:
    evidence = []
    for item in evidence_packet.get("evidence", [])[:8]:
        if isinstance(item, dict):
            evidence.append(
                {
                    "path": item.get("path"),
                    "lines": f"{item.get('line_start')}-{item.get('line_end')}",
                    "content": item.get("content", "")[:800],
                }
            )
    return {
        "summary": evidence_packet.get("summary", {}),
        "files_inspected": evidence_packet.get("summary", {}).get("files_inspected", 0),
        "evidence": evidence,
    }


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- NONE"


def _forbidden_files(allowed_files: list[str]) -> list[str]:
    files = [".env", ".env.*", "credentials", "secrets", "*.pem", "*.key"]
    files.extend(item for item in allowed_files if is_secret_path(item))
    return sorted(set(files))


def _forbidden_areas(review: Review, complexity: ComplexityAssessment) -> list[str]:
    areas = ["private keys", "path traversal", "../", "absolute paths"]
    if complexity.severity in {"HARD", "CRITICAL"}:
        areas.append("security-sensitive code")
    if review.patch_required:
        areas.append("patch-required area")
    return areas


def _normalize_response_text(response_text: str) -> str:
    text = response_text.replace("\r\n", "\n").strip()
    terminator = DEFAULT_TERMINATOR
    if terminator in text:
        text = text.split(terminator, 1)[0].strip()
    return text


def _extract_fenced_patch(text: str) -> str | None:
    fences = re.findall(r"```(?:diff|patch)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if not fences:
        return None
    for block in fences:
        cleaned = block.strip()
        if _looks_like_diff(cleaned):
            return cleaned
    return None


def _find_patch_start(text: str) -> int:
    for pattern in (r"(?m)^diff --git ", r"(?m)^--- a/"):
        match = re.search(pattern, text)
        if match:
            return match.start()
    return -1


def _looks_like_diff(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return any(line.startswith("diff --git ") for line in lines) or any(line.startswith("--- a/") for line in lines)


def _extract_changed_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            path = line.split(" ", 1)[1].strip()
            if path == "/dev/null":
                continue
            if path.startswith("a/") or path.startswith("b/"):
                path = path[2:]
            paths.append(path)
        if line.startswith("rename to ") or line.startswith("rename from "):
            paths.append(line.split(" ", 2)[2].strip())
    deduped: list[str] = []
    for item in paths:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _is_safe_relative_path(path_text: str) -> bool:
    if not path_text or path_text in {"/dev/null"}:
        return True
    normalized = path_text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    pure = PurePosixPath(normalized)
    return ".." not in pure.parts and not any(part == "" for part in pure.parts)


def _contains_secret_material(text: str) -> bool:
    lowered = text.lower()
    if any(token in lowered for token in ("api_key", "secret", "authorization", "bearer ")):
        return True
    return bool(re.search(r"AIza[0-9A-Za-z_\-]{20,}", text) or re.search(r"sk-[0-9A-Za-z_\-]{20,}", text) or re.search(r"nvapi-[0-9A-Za-z_\-]{20,}", text))


def _looks_like_binary_patch(text: str) -> bool:
    lowered = text.lower()
    return "binary files" in lowered or "git binary patch" in lowered
