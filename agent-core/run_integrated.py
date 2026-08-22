from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from observability import capture_exception, init_observability, observe_run
from orchestration import (
    CrewAIUnavailableError,
    GoalPipelineEngine,
    PipelineRequest,
    PipelineRunResult,
    PipelineStageError,
    run_crewai_flow,
)
from production_preflight import ProductionPreflightError, run_production_preflight


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpenClaw/CrewAI/OpenHands integrated goal runner."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--goal-id", help="Resume an existing goal.")
    source.add_argument("--task", help="Create and run a new goal.")
    parser.add_argument("--repo", help="Target git repository for a new task.")
    parser.add_argument("--runtime-dir", default="runtime/goals")
    parser.add_argument("--execution-mode", default=None)
    parser.add_argument("--commander-profile", default=None)
    parser.add_argument("--openhands-python", default=None)
    parser.add_argument(
        "--orchestrator",
        choices=["crewai", "native", "temporal"],
        default=os.getenv("AGENT_ARMY_ORCHESTRATOR", "crewai"),
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="Explicitly allow the final patch to be applied to the target repo.",
    )
    parser.add_argument(
        "--mvp-unrestricted",
        action="store_true",
        help="Temporarily bypass application-level file-scope gates while preserving the multi-agent chain and verification.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.task and not args.repo:
        parser.error("--repo is required with --task")

    if args.mvp_unrestricted:
        os.environ["AGENT_ARMY_MVP_UNRESTRICTED"] = "true"
        os.environ["AGENT_ARMY_OPENHANDS_ONLY"] = "true"

    # Integrated runs default to isolated OpenHands + verification. Unit tests and
    # low-level development entry points can explicitly opt back into local mode.
    os.environ.setdefault("AGENT_ARMY_OPENHANDS_WORKSPACE", "docker")
    os.environ.setdefault("AGENT_ARMY_VERIFICATION_SANDBOX", "docker")

    core_dir = Path(__file__).resolve().parent
    try:
        preflight = run_production_preflight(
            orchestrator=args.orchestrator,
            core_dir=core_dir,
            openhands_python=args.openhands_python,
        )
    except ProductionPreflightError as exc:
        print(f"PREFLIGHT_FAILED: {exc}", file=sys.stderr)
        return 3

    request = PipelineRequest(
        repo=args.repo,
        task=args.task,
        goal_id=args.goal_id,
        runtime_dir=args.runtime_dir,
        execution_mode=args.execution_mode,
        commander_profile=args.commander_profile,
        openhands_python=args.openhands_python,
        auto_apply=args.auto_apply,
    )

    init_observability()
    metadata = {
        "orchestrator": args.orchestrator,
        "auto_apply": args.auto_apply,
        "resume": bool(args.goal_id),
        "preflight": preflight.get("preflight", "unknown"),
        "mvp_unrestricted": args.mvp_unrestricted,
    }

    try:
        with observe_run("agent-army-pipeline", metadata=metadata):
            result = _run(request, args.orchestrator)
    except CrewAIUnavailableError as exc:
        capture_exception(exc, orchestrator=args.orchestrator)
        print(f"CREWAI_UNAVAILABLE: {exc}", file=sys.stderr)
        return 4
    except PipelineStageError as exc:
        capture_exception(exc, orchestrator=args.orchestrator, stage=exc.stage)
        print(f"PIPELINE_FAILED stage={exc.stage}: {exc}", file=sys.stderr)
        return 5
    except Exception as exc:
        capture_exception(exc, orchestrator=args.orchestrator)
        print(f"PIPELINE_FAILED: {exc}", file=sys.stderr)
        return 6

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("INTEGRATED AGENT PIPELINE")
        print(f"Goal: {result.goal_id}")
        print(f"State: {result.state}")
        print("Orchestrator:", args.orchestrator)
        print("Builder: OpenHands (Docker isolated by default)")
        print("OpenClaw entry: supported via openclaw/skills/agent-army")
        print(f"Applied: {'YES' if result.applied else 'NO'}")
        if result.next_action:
            print(f"Next action: {result.next_action}")
    return 0


def _run(request: PipelineRequest, orchestrator: str) -> PipelineRunResult:
    if orchestrator == "temporal":
        try:
            from orchestration.temporal_flow import run_temporal_pipeline
        except ImportError as exc:
            raise RuntimeError(
                "Temporal is not installed. Install agent-core/requirements-reliability.txt."
            ) from exc
        payload = asyncio.run(run_temporal_pipeline(asdict(request)))
        return PipelineRunResult(**payload)

    engine = GoalPipelineEngine(request)
    if orchestrator == "crewai":
        return run_crewai_flow(engine)
    return engine.run_native()


if __name__ == "__main__":
    raise SystemExit(main())
