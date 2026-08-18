from __future__ import annotations

from pathlib import Path
from typing import Any

from evidence.sanitizer import redact_text
from schemas import Review, Verdict


MAX_TARGET_FILES = 5
MAX_EVIDENCE_SNIPPETS = 8
MAX_LINES_PER_SNIPPET = 80
MAX_HANDOFF_BYTES = 20 * 1024


def build_codex_handoff(
    task: str,
    repo: Path,
    review: Review,
    analyst_results: list[Verdict],
    evidence_packet: dict[str, Any],
    output_path: Path,
) -> Path:
    snippets = _collect_snippets(evidence_packet)
    target_files = _target_files(snippets, review)
    content = _render(task, repo, review, analyst_results, target_files, snippets)
    content = _fit_limit(redact_text(content), MAX_HANDOFF_BYTES)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def _collect_snippets(evidence_packet: dict[str, Any]) -> list[dict[str, str]]:
    snippets: list[dict[str, str]] = []
    for item in evidence_packet.get("evidence", []):
        if len(snippets) >= MAX_EVIDENCE_SNIPPETS:
            return snippets
        path = str(item.get("path", ""))
        start = int(item.get("line_start", 0) or 0)
        end = int(item.get("line_end", 0) or 0)
        if start and end and end - start + 1 > MAX_LINES_PER_SNIPPET:
            end = start + MAX_LINES_PER_SNIPPET - 1
        text = str(item.get("content", ""))
        snippets.append({"path": path, "lines": f"{start}-{end}", "text": text})
    return snippets


def _target_files(snippets: list[dict[str, str]], review: Review) -> list[str]:
    paths: list[str] = []
    for item in list(review.evidence) + snippets:
        path = str(item.get("path", ""))
        if path and path not in paths:
            paths.append(path)
        if len(paths) >= MAX_TARGET_FILES:
            break
    return paths


def _render(
    task: str,
    repo: Path,
    review: Review,
    analyst_results: list[Verdict],
    target_files: list[str],
    snippets: list[dict[str, str]],
) -> str:
    failing_tests = _failing_tests(snippets)
    acceptance = "\n".join(f"- {item}" for item in _acceptance_criteria(review))
    targets = "\n".join(f"- {path}" for path in target_files) or "- NOT_IDENTIFIED"
    line_ranges = "\n".join(f"- {item['path']}:{item['lines']}" for item in snippets) or "- NOT_IDENTIFIED"
    evidence = "\n".join(
        f"- {item['path']}:{item['lines']}\n\n```text\n{_trim_lines(item['text'])}\n```"
        for item in snippets
    ) or "- No compact evidence snippet available."
    tests = "\n".join(f"- {item}" for item in failing_tests) or "- Not identified from evidence."
    analyst_summary = "\n".join(
        f"- {item.analyst or 'analyst'} ({item.profile or 'profile'}): {item.verdict}, confidence={item.confidence:.2f}"
        for item in analyst_results
    )
    return f"""# Codex Patch Task

## Goal
{task}

## Problem
Reviewer final verdict is {review.final_verdict}. Patch only if the issue is directly supported by the listed evidence.

## Final Verdict
{review.final_verdict}

Patch required: {str(review.patch_required).lower()}

Analyst summary:
{analyst_summary}

## Acceptance Criteria
{acceptance}

## Target Files
{targets}

## Relevant Evidence
{evidence}

## Relevant Line Ranges
{line_ranges}

## Failing Tests
{tests}

## Allowed Changes
- Inspect only the listed files and line ranges first.
- Make the minimum patch required to satisfy the acceptance criteria.
- Add or update narrowly scoped tests only when needed.

## Forbidden Changes
- Do not do repo-wide search unless the listed evidence is insufficient.
- Do not refactor unrelated code.
- Do not change secrets, credentials, .env files, API keys, generated artifacts, or large data files.
- Do not change strategy/model/universe parameters unless explicitly named in the evidence.

## Verification Commands
- Run the smallest relevant test command discovered from the target files.
- If no test command is evident, run syntax/type checks for changed files only.

Repository: {repo}
"""


def _acceptance_criteria(review: Review) -> list[str]:
    if review.final_verdict == "FAIL":
        return [
            "Fix the specific failing behavior identified by the reviewer.",
            "Keep the patch minimal and evidence-bound.",
            "Verification command passes.",
        ]
    return ["No patch should be produced unless new direct evidence proves a concrete failure."]


def _failing_tests(snippets: list[dict[str, str]]) -> list[str]:
    tests: list[str] = []
    for item in snippets:
        text = item["text"].lower()
        if "fail" in text or "error" in text:
            tests.append(f"{item['path']}:{item['lines']}")
    return tests[:5]


def _trim_lines(text: str) -> str:
    lines = text.splitlines()[:MAX_LINES_PER_SNIPPET]
    return "\n".join(lines)


def _fit_limit(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix = "\n\n[TRUNCATED_TO_20KB]\n"
    keep = max_bytes - len(suffix.encode("utf-8"))
    return encoded[:keep].decode("utf-8", errors="ignore") + suffix
