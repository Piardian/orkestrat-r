from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any

from analyst import Analyst
from commander import Commander
from evidence.builder import EvidenceBuilder
from llm.client import LLMError
from llm.router import LLMRouter
from reviewer import Reviewer
from schemas import Verdict


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Commander -> Evidence -> Analyst A/B -> Reviewer chain.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--commander-profile", default="gemini-user-a")
    parser.add_argument("--analyst-a-profile", default="gemini-user-a")
    parser.add_argument("--analyst-b-profile", default="gemini-user-b")
    parser.add_argument("--reviewer-profile", default="gemini-user-a")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    output_dir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="agent-core-multi-"))
    output_dir.mkdir(parents=True, exist_ok=True)

    telemetry = _empty_telemetry()
    router = LLMRouter()

    try:
        commander_client = router.get_client(args.commander_profile)
        telemetry["commander_calls"] = 1
        plan, commander_usage = Commander(commander_client).create_plan(args.task, repo.name)
        _merge_usage(telemetry, "commander", commander_usage)
    except (LLMError, Exception) as exc:
        _print_error("commander", args.commander_profile, exc)
        return 2

    (output_dir / "search_plan.json").write_text(
        json.dumps(plan.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    evidence_packet = EvidenceBuilder(repo, plan.to_dict()).build()
    evidence_path = output_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence_packet, indent=2, ensure_ascii=False), encoding="utf-8")
    telemetry["evidence_builds"] = 1
    telemetry["evidence_bytes"] = evidence_path.stat().st_size
    telemetry["files_inspected"] = evidence_packet["summary"]["files_inspected"]
    telemetry["lines_captured"] = evidence_packet["summary"]["lines_captured"]

    analyst_results: list[Verdict] = []
    partial_failure = False
    analyst_specs = [
        ("analyst-a", args.analyst_a_profile, "evidence-first evaluator"),
        ("analyst-b", args.analyst_b_profile, "skeptical verifier"),
    ]
    for analyst_id, profile_id, perspective in analyst_specs:
        stage_key = analyst_id.replace("-", "_")
        try:
            client = router.get_client(profile_id)
            telemetry[f"{stage_key}_calls"] = 1
            verdict, usage = Analyst(client).analyze(args.task, evidence_packet, analyst_id, profile_id, perspective)
            if not verdict.analyst:
                verdict = Verdict(
                    verdict=verdict.verdict,
                    confidence=verdict.confidence,
                    reason=verdict.reason,
                    evidence=verdict.evidence,
                    analyst=analyst_id,
                    profile=profile_id,
                    uncertainties=verdict.uncertainties,
                )
            analyst_results.append(verdict)
            _merge_usage(telemetry, stage_key, usage)
        except (LLMError, Exception) as exc:
            partial_failure = True
            _print_error(analyst_id, profile_id, exc)

    if not analyst_results:
        print("No analyst result available; reviewer skipped.")
        return 3

    try:
        reviewer_client = router.get_client(args.reviewer_profile)
        telemetry["reviewer_calls"] = 1
        status = "DEGRADED" if partial_failure else "OK"
        review, reviewer_usage = Reviewer(reviewer_client).review(args.task, analyst_results, status=status)
        _merge_usage(telemetry, "reviewer", reviewer_usage)
    except (LLMError, Exception) as exc:
        _print_error("reviewer", args.reviewer_profile, exc)
        return 4

    (output_dir / "analyst_results.json").write_text(
        json.dumps([item.to_dict() for item in analyst_results], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "final_review.json").write_text(
        json.dumps(review.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    _print_summary(args, telemetry, analyst_results, review, partial_failure, output_dir)
    return 0


def _empty_telemetry() -> dict[str, Any]:
    telemetry: dict[str, Any] = {
        "commander_calls": 0,
        "analyst_a_calls": 0,
        "analyst_b_calls": 0,
        "reviewer_calls": 0,
        "evidence_builds": 0,
        "retry_count": 0,
        "evidence_bytes": 0,
        "files_inspected": 0,
        "lines_captured": 0,
    }
    for stage in ("commander", "analyst_a", "analyst_b", "reviewer"):
        telemetry[f"{stage}_input_tokens"] = 0
        telemetry[f"{stage}_output_tokens"] = 0
    return telemetry


def _merge_usage(telemetry: dict[str, Any], stage: str, usage: dict[str, Any]) -> None:
    telemetry["retry_count"] += int(usage.get("retry_count") or 0)
    telemetry[f"{stage}_input_tokens"] += int(usage.get("input_tokens") or 0)
    telemetry[f"{stage}_output_tokens"] += int(usage.get("output_tokens") or 0)


def _print_error(stage: str, profile: str, exc: Exception) -> None:
    kind = exc.kind if isinstance(exc, LLMError) else exc.__class__.__name__
    retry_count = exc.retry_count if isinstance(exc, LLMError) else 0
    print("ERROR")
    print(f"stage={stage}")
    print(f"type={kind}")
    print(f"profile={profile}")
    print(f"retry_count={retry_count}")
    print(str(exc))


def _print_summary(args: argparse.Namespace, telemetry: dict[str, Any], results: list[Verdict], review, partial_failure: bool, output_dir: Path) -> None:
    total_calls = telemetry["commander_calls"] + telemetry["analyst_a_calls"] + telemetry["analyst_b_calls"] + telemetry["reviewer_calls"]
    total_input = sum(telemetry[f"{stage}_input_tokens"] for stage in ("commander", "analyst_a", "analyst_b", "reviewer"))
    total_output = sum(telemetry[f"{stage}_output_tokens"] for stage in ("commander", "analyst_a", "analyst_b", "reviewer"))
    print(f"Task:\n{args.task}\n")
    print("Profiles:")
    print(f"Commander: {args.commander_profile}")
    print(f"Analyst A: {args.analyst_a_profile}")
    print(f"Analyst B: {args.analyst_b_profile}")
    print(f"Reviewer: {args.reviewer_profile}\n")
    print("Evidence:")
    print(f"Builds: {telemetry['evidence_builds']}")
    print(f"Files: {telemetry['files_inspected']}")
    print(f"Lines: {telemetry['lines_captured']}")
    print(f"Size: {telemetry['evidence_bytes']} bytes\n")
    for item in results:
        print(f"{item.analyst or 'analyst'}:")
        print(f"{item.verdict}")
        print(f"confidence={item.confidence:.2f}\n")
    print("Reviewer:")
    print(f"FINAL: {review.final_verdict}")
    print(f"AGREEMENT: {review.agreement}")
    print(f"confidence={review.confidence:.2f}\n")
    print("LLM CALLS:")
    print(f"Commander: {telemetry['commander_calls']}")
    print(f"Analyst A: {telemetry['analyst_a_calls']}")
    print(f"Analyst B: {telemetry['analyst_b_calls']}")
    print(f"Reviewer: {telemetry['reviewer_calls']}")
    print(f"Total: {total_calls}\n")
    print("TOKEN USAGE")
    for stage in ("commander", "analyst_a", "analyst_b", "reviewer"):
        print(f"{stage}: {telemetry[f'{stage}_input_tokens']} in / {telemetry[f'{stage}_output_tokens']} out")
    print(f"TOTAL: {total_input} input / {total_output} output")
    print(f"Partial failure: {'YES' if partial_failure else 'NO'}")
    print(f"429: {'YES' if telemetry['retry_count'] else 'NO'}")
    print(f"Output dir: {output_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
