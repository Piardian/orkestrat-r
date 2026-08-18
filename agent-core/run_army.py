from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any

import yaml

from analyst import Analyst
from budget import BudgetTracker
from commander import Commander
from evidence.builder import EvidenceBuilder
from handoff import build_codex_handoff
from llm.client import LLMError
from llm.router import LLMRouter
from llm.execution import load_execution_policy, resolve_output_tokens
from reviewer import Reviewer
from schemas import Review, Verdict


ROLE_PERSPECTIVES = {
    "evidence-first": "evidence-first evaluator: direct evidence, implementation, acceptance criterion",
    "skeptical": "skeptical verifier: missing evidence, false positives, edge cases",
    "adversarial": "adversarial verifier: counterarguments, failure modes, hidden assumptions",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run config-driven N-agent army skeleton.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--level", choices=["simple", "standard", "critical"], default="standard")
    parser.add_argument("--config", default="config/army.yaml")
    parser.add_argument("--commander-profile", default=None)
    parser.add_argument("--execution-mode", default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--simulate-patch-required", action="store_true")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    config = _load_config(Path(args.config))
    repo = Path(args.repo).resolve()
    output_dir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="agent-core-army-"))
    output_dir.mkdir(parents=True, exist_ok=True)

    policy = config["execution_policy"][args.level]
    execution_policy = load_execution_policy(config, args.execution_mode)
    token_limit = config.get("token_policy", {}).get(args.level, {}).get("max_total_input_tokens")
    budget = BudgetTracker(max_total_input_tokens=token_limit)
    router = LLMRouter(env_path=args.env_file)
    profile_models = {item["id"]: item["model"] for item in router.registry.list_profiles()}
    stage_tokens: dict[str, dict[str, int]] = {}
    failures: list[dict[str, str]] = []

    commander_cfg = dict(config["commander"])
    if args.commander_profile:
        commander_cfg["profile"] = args.commander_profile
    if not commander_cfg.get("enabled", True):
        print("Commander disabled by config.")
        return 2

    try:
        commander_profile = router.registry.get(commander_cfg["profile"])
        commander_client = router.get_client(commander_cfg["profile"], execution_policy)
        budget.add_call()
        plan, usage = Commander(commander_client, execution_policy).create_plan(
            args.task,
            repo.name,
            max_tokens=resolve_output_tokens(commander_profile, 256, execution_policy),
        )
        budget.add_usage(usage)
        stage_tokens["commander"] = _usage_pair(usage)
    except (LLMError, Exception) as exc:
        if isinstance(exc, LLMError):
            budget.add_failed_provider_request(exc.kind, exc.retry_count)
        _record_failure(failures, "commander", commander_cfg["profile"], exc)
        _print_failures(failures)
        return 2

    evidence_budget = config.get("budgets", {}).get("evidence", {})
    plan_dict = plan.to_dict()
    plan_dict["max_files"] = min(int(evidence_budget.get("max_files", 5)), plan_dict["max_files"])
    plan_dict["max_lines_per_file"] = min(
        int(evidence_budget.get("max_lines_per_file", 80)),
        plan_dict["max_lines_per_file"],
    )

    (output_dir / "search_plan.json").write_text(json.dumps(plan_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    evidence_packet = EvidenceBuilder(repo, plan_dict).build()
    evidence_path = output_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence_packet, indent=2, ensure_ascii=False), encoding="utf-8")
    budget.evidence_bytes = evidence_path.stat().st_size
    evidence_max_bytes = int(evidence_budget.get("max_bytes", 20000))
    evidence_over_budget = budget.evidence_bytes > evidence_max_bytes

    enabled_analysts = [item for item in config.get("analysts", []) if item.get("enabled", True)]
    requested_analysts = enabled_analysts[: int(policy["analysts"])]
    analyst_results: list[Verdict] = []

    for analyst_cfg in requested_analysts:
        if not budget.can_run_next_agent():
            _record_budget_failure(failures, analyst_cfg["id"], analyst_cfg["profile"])
            continue
        stage_name = analyst_cfg["id"]
        try:
            analyst_profile = router.registry.get(analyst_cfg["profile"])
            client = router.get_client(analyst_cfg["profile"], execution_policy)
            budget.add_call()
            role = analyst_cfg.get("role", "evidence-first")
            perspective = ROLE_PERSPECTIVES.get(role, role)
            verdict, usage = Analyst(client, execution_policy).analyze(
                args.task,
                evidence_packet,
                analyst_id=stage_name,
                profile_id=analyst_cfg["profile"],
                perspective=perspective,
                max_tokens=resolve_output_tokens(analyst_profile, 1600, execution_policy),
            )
            if not verdict.analyst:
                verdict = Verdict(
                    verdict=verdict.verdict,
                    confidence=verdict.confidence,
                    reason=verdict.reason,
                    evidence=verdict.evidence,
                    analyst=stage_name,
                    profile=analyst_cfg["profile"],
                    uncertainties=verdict.uncertainties,
                )
            analyst_results.append(verdict)
            budget.add_usage(usage)
            stage_tokens[stage_name] = _usage_pair(usage)
        except (LLMError, Exception) as exc:
            if isinstance(exc, LLMError):
                budget.add_failed_provider_request(exc.kind, exc.retry_count)
            _record_failure(failures, stage_name, analyst_cfg["profile"], exc)

    quorum_required = int(config.get("quorum", {}).get(args.level, 1))
    quorum_met = len(analyst_results) >= quorum_required
    reviewer_enabled = bool(policy.get("reviewer")) and config.get("reviewer", {}).get("enabled", True)
    planned_logical_stages = 1 + len(requested_analysts) + (1 if reviewer_enabled else 0)

    if reviewer_enabled and analyst_results and budget.can_run_next_agent():
        reviewer_cfg = config["reviewer"]
        try:
            reviewer_profile = router.registry.get(reviewer_cfg["profile"])
            client = router.get_client(reviewer_cfg["profile"], execution_policy)
            budget.add_call()
            status = "OK" if not failures and quorum_met and not evidence_over_budget else "DEGRADED"
            review, usage = Reviewer(client, execution_policy).review(
                args.task,
                analyst_results,
                status=status,
                max_tokens=resolve_output_tokens(reviewer_profile, 2400, execution_policy),
            )
            budget.add_usage(usage)
            stage_tokens["reviewer"] = _usage_pair(usage)
        except (LLMError, Exception) as exc:
            if isinstance(exc, LLMError):
                budget.add_failed_provider_request(exc.kind, exc.retry_count)
            _record_failure(failures, "reviewer", config["reviewer"]["profile"], exc)
            review = _fallback_review(analyst_results, quorum_met)
    else:
        review = _fallback_review(analyst_results, quorum_met)

    if args.simulate_patch_required:
        review = Review(
            "FAIL",
            review.confidence,
            review.agreement,
            f"Simulated patch-required flow. Original reviewer reason: {review.reason}",
            review.analyst_a,
            review.analyst_b,
            review.analysts,
            review.evidence,
            True,
        )

    execution_status = _execution_status(quorum_met, failures, evidence_over_budget)
    patching_cfg = config.get("patching", {"mode": "disabled"})
    handoff_path = None
    if review.patch_required and patching_cfg.get("mode") == "manual-codex":
        handoff_path = build_codex_handoff(
            args.task,
            repo,
            review,
            analyst_results,
            evidence_packet,
            Path("artifacts") / "codex_handoff.md",
        )
    result = {
        "task": args.task,
        "level": args.level,
        "execution_status": execution_status,
        "final_verdict": review.final_verdict,
        "confidence": review.confidence,
        "agreement": review.agreement,
        "analysts_requested": len(requested_analysts),
        "analysts_completed": len(analyst_results),
        "analysts_failed": len(requested_analysts) - len(analyst_results),
        "quorum_required": quorum_required,
        "quorum_met": quorum_met,
        "evidence_bytes": budget.evidence_bytes,
        "budget": budget.to_dict(),
        "planned_logical_stages": planned_logical_stages,
        "successful_stages": len(stage_tokens),
        "failed_stages": len(failures),
        "failures": failures,
        "profiles": {
            "commander": commander_cfg["profile"],
            "commander_model": profile_models.get(commander_cfg["profile"]),
            "analysts": [item["profile"] for item in requested_analysts],
            "analyst_models": [profile_models.get(item["profile"]) for item in requested_analysts],
            "reviewer": config["reviewer"]["profile"],
            "reviewer_model": profile_models.get(config["reviewer"]["profile"]),
        },
        "patch_required": review.patch_required,
        "patch_mode": patching_cfg.get("mode", "disabled"),
        "codex_handoff": str(handoff_path) if handoff_path else None,
    }
    (output_dir / "army_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "analyst_results.json").write_text(
        json.dumps([item.to_dict() for item in analyst_results], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "final_review.json").write_text(
        json.dumps(review.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    _print_summary(args, config, commander_cfg, requested_analysts, evidence_packet, budget, stage_tokens, failures, review, result)
    return 0 if quorum_met else 5


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _usage_pair(usage: dict[str, Any]) -> dict[str, int]:
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }


def _record_failure(failures: list[dict[str, str]], stage: str, profile: str, exc: Exception) -> None:
    kind = exc.kind if isinstance(exc, LLMError) else exc.__class__.__name__
    failures.append({"stage": stage, "profile": profile, "type": str(kind), "message": str(exc)})


def _record_budget_failure(failures: list[dict[str, str]], stage: str, profile: str) -> None:
    failures.append({"stage": stage, "profile": profile, "type": "TOKEN_BUDGET", "message": "Skipped by budget guard"})


def _print_failures(failures: list[dict[str, str]]) -> None:
    for failure in failures:
        print("ERROR")
        print(f"stage={failure['stage']}")
        print(f"type={failure['type']}")
        print(f"profile={failure['profile']}")
        print(failure["message"])


def _fallback_review(results: list[Verdict], quorum_met: bool) -> Review:
    if not results:
        return Review("UNKNOWN", 0.0, "PARTIAL", "No analyst result available.", {}, None, [], [])
    first = results[0]
    agreement = "PARTIAL" if quorum_met else "CONFLICT"
    return Review(
        first.verdict if quorum_met else "UNKNOWN",
        first.confidence if quorum_met else 0.0,
        agreement,
        "Fallback review from available analyst results.",
        first.to_dict(),
        results[1].to_dict() if len(results) > 1 else None,
        [item.to_dict() for item in results],
        first.evidence,
    )


def _execution_status(quorum_met: bool, failures: list[dict[str, str]], evidence_over_budget: bool) -> str:
    if not quorum_met:
        return "INSUFFICIENT_ANALYSTS"
    if evidence_over_budget or failures:
        return "DEGRADED"
    return "SUCCESS"


def _print_summary(args, config, commander_cfg, requested_analysts, evidence_packet, budget, stage_tokens, failures, review, result) -> None:
    total_input = sum(item["input"] for item in stage_tokens.values())
    total_output = sum(item["output"] for item in stage_tokens.values())
    reviewer_calls = 1 if "reviewer" in stage_tokens else 0
    print("ARMY EXECUTION\n")
    print(f"Level:\n{args.level}\n")
    print("Profiles:")
    print(f"Commander: {commander_cfg['profile']}")
    print(f"Commander model: {result['profiles']['commander_model']}")
    for analyst in requested_analysts:
        print(f"{analyst['id']}: {analyst['profile']}")
    print(f"Reviewer: {config['reviewer']['profile']}\n")
    print("Evidence:")
    print(f"Files: {evidence_packet['summary']['files_inspected']}")
    print(f"Lines: {evidence_packet['summary']['lines_captured']}")
    print(f"Size: {budget.evidence_bytes} bytes\n")
    print("LLM CALLS:")
    print(f"Planned logical stages: {result['planned_logical_stages']}")
    print(f"Successful stages: {result['successful_stages']}")
    print(f"Failed stages: {result['failed_stages']}")
    print(f"Commander stages: {1 if 'commander' in stage_tokens else 0}")
    print(f"Analyst stages: {len([k for k in stage_tokens if k.startswith('analyst-')])}")
    print(f"Reviewer stages: {reviewer_calls}")
    print(f"Provider requests: {budget.provider_requests}")
    print(f"Format repairs: {budget.repair_count}")
    print(f"Stage regenerations: {budget.stage_regeneration_count}")
    print(f"Provider retries: {budget.retry_count}\n")
    print("TOKENS:")
    print(f"Input: {total_input}")
    print(f"Output: {total_output}\n")
    print(f"Failures: {len(failures)}")
    saw_429 = budget.rate_limit_retry_count > 0 or any(item["type"] == "RATE_LIMIT" for item in failures)
    print(f"429: {'YES' if saw_429 else 'NO'}\n")
    print(f"Service unavailable retries: {budget.service_unavailable_retry_count}")
    print(f"Patch required: {'YES' if review.patch_required else 'NO'}")
    print(f"Patch mode: {result['patch_mode']}")
    print(f"Codex handoff: {'generated' if result['codex_handoff'] else 'not-generated'}")
    if result["codex_handoff"]:
        print(f"Codex handoff path: {result['codex_handoff']}")
        print("\nPATCH REQUIRED")
        print(f"Codex handoff generated:\n{result['codex_handoff']}")
        print("\nManual Codex execution required.")
    print()
    print("FINAL:")
    print(review.final_verdict)
    print(f"Agreement: {review.agreement}")
    print(f"Execution status: {result['execution_status']}")
    print(f"Quorum: {result['analysts_completed']} / {result['quorum_required']}")


if __name__ == "__main__":
    raise SystemExit(main())
