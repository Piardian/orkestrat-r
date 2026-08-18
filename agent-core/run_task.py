from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from analyst import Analyst
from commander import Commander
from evidence.builder import EvidenceBuilder
from llm.client import LLMError
from llm.router import LLMRouter


def main() -> int:
    parser = argparse.ArgumentParser(description="Run controlled Commander -> Evidence -> Analyst chain.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--commander-profile", default="gemini-user-a")
    parser.add_argument("--analyst-profile", default="gemini-user-a")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    output_dir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="agent-core-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "search_plan.json"
    evidence_path = output_dir / "evidence.json"

    telemetry = {
        "commander_calls": 0,
        "analyst_calls": 0,
        "retry_count": 0,
        "evidence_bytes": 0,
        "files_inspected": 0,
        "lines_captured": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }

    router = LLMRouter()
    try:
        commander_client = router.get_client(args.commander_profile)
        telemetry["commander_calls"] += 1
        plan, commander_usage = Commander(commander_client).create_plan(args.task, repo.name)
        _merge_usage(telemetry, commander_usage)
    except LLMError as exc:
        _print_stage_error("commander", args.commander_profile, exc)
        return 2

    plan_path.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    evidence_packet = EvidenceBuilder(repo, plan.to_dict()).build()
    evidence_path.write_text(json.dumps(evidence_packet, indent=2, ensure_ascii=False), encoding="utf-8")

    telemetry["evidence_bytes"] = evidence_path.stat().st_size
    telemetry["files_inspected"] = evidence_packet["summary"]["files_inspected"]
    telemetry["lines_captured"] = evidence_packet["summary"]["lines_captured"]

    try:
        analyst_client = router.get_client(args.analyst_profile)
        telemetry["analyst_calls"] += 1
        verdict, analyst_usage = Analyst(analyst_client).analyze(args.task, evidence_packet)
        _merge_usage(telemetry, analyst_usage)
    except LLMError as exc:
        _print_stage_error("analyst", args.analyst_profile, exc)
        return 3

    print(f"Task:\n{args.task}\n")
    print(f"Commander:\n{len(plan.search_terms)} search terms generated\n")
    print(
        "Evidence:\n"
        f"{telemetry['files_inspected']} files inspected\n"
        f"{telemetry['lines_captured']} lines captured\n"
        f"{telemetry['evidence_bytes']} bytes\n"
    )
    print("Analyst:")
    print(f"VERDICT: {verdict.verdict}")
    print(f"CONFIDENCE: {verdict.confidence:.2f}\n")
    print(f"Reason:\n{verdict.reason}\n")
    print("Evidence:")
    for item in verdict.evidence:
        print(f"- {item['path']}:{item['lines']}")
    print("\nLLM CALLS:")
    print(f"Commander: {telemetry['commander_calls']}")
    print(f"Analyst: {telemetry['analyst_calls']}")
    print(f"Total: {telemetry['commander_calls'] + telemetry['analyst_calls']}")
    if telemetry["input_tokens"] or telemetry["output_tokens"]:
        print(f"Tokens input/output: {telemetry['input_tokens']} / {telemetry['output_tokens']}")
    print(f"Output dir: {output_dir}")
    return 0


def _merge_usage(telemetry: dict, usage: dict) -> None:
    telemetry["retry_count"] += int(usage.get("retry_count") or 0)
    telemetry["input_tokens"] += int(usage.get("input_tokens") or 0)
    telemetry["output_tokens"] += int(usage.get("output_tokens") or 0)


def _print_stage_error(stage: str, profile: str, exc: LLMError) -> None:
    print("ERROR:")
    print(f"stage={stage}")
    print(f"type={exc.kind}")
    print(f"profile={profile}")
    print(f"retry_count={exc.retry_count}")
    print(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
