from __future__ import annotations

import asyncio
from datetime import timedelta
import os
from pathlib import Path
from typing import Any, Callable

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_temporal_postgres() -> None:
    """Fail closed when durable execution is configured without durable state.

    Temporal activity retries are only safe across worker/process boundaries when
    all workers share the PostgreSQL goal source-of-truth. Development can opt out
    explicitly with AGENT_ARMY_TEMPORAL_REQUIRE_POSTGRES=false.
    """

    if not _truthy(os.getenv("AGENT_ARMY_TEMPORAL_REQUIRE_POSTGRES"), default=True):
        return
    backend = os.getenv("AGENT_ARMY_STATE_BACKEND", "auto").strip().lower()
    database_url = os.getenv("AGENT_ARMY_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
    if backend in {"file", "filesystem"}:
        raise RuntimeError("Temporal production mode requires PostgreSQL; filesystem state backend is not allowed.")
    if backend not in {"auto", "postgres"}:
        raise RuntimeError(f"Temporal production mode does not support state backend: {backend}")
    if not database_url:
        raise RuntimeError(
            "Temporal production mode requires AGENT_ARMY_DATABASE_URL (or DATABASE_URL) so retries share durable state."
        )


async def _run_with_heartbeat(stage: str, operation: Callable[[], Any]) -> Any:
    """Run blocking pipeline code in a worker thread while heartbeating Temporal."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    while not task.done():
        activity.heartbeat({"stage": stage})
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
        except asyncio.TimeoutError:
            continue
    return await task


@activity.defn
async def run_pipeline_stage(payload: dict[str, Any]) -> dict[str, Any]:
    # Imports stay inside the activity so Temporal workflow sandbox remains deterministic.
    require_temporal_postgres()
    from orchestration.engine import GoalPipelineEngine, PipelineRequest

    stage = str(payload["stage"])
    request_data = dict(payload["request"])
    request_key = str(request_data.pop("_request_id", "") or "").strip()
    goal_id = payload.get("goal_id")
    if goal_id:
        request_data["goal_id"] = goal_id
        request_data["repo"] = None
        request_data["task"] = None

    engine = GoalPipelineEngine(PipelineRequest(**request_data))

    if stage == "intake":
        def _intake() -> str:
            if engine.request.goal_id:
                return engine.create_or_resume()
            if not engine.request.repo or not engine.request.task:
                raise ValueError("repo and task are required for Temporal intake")
            record = engine.service.create_goal(
                engine.request.task,
                Path(engine.request.repo),
                idempotency_key=request_key or None,
            )
            return record.goal_id

        goal_id = await _run_with_heartbeat(stage, _intake)
        return {"goal_id": goal_id, "state": engine.service.read_goal(goal_id).status}

    if not goal_id:
        raise ValueError(f"goal_id is required for stage {stage}")

    operations = {
        "plan": engine.plan,
        "plan_review": engine.review_plan,
        "complexity": engine.assess_complexity,
        "build": engine.build,
        "final_review": engine.final_review,
        "apply": engine.apply,
    }
    if stage == "finish":
        return engine.finish(goal_id).to_dict()
    if stage not in operations:
        raise ValueError(f"unknown stage: {stage}")

    def _locked_stage() -> str:
        with engine.service.store.goal_lock(goal_id):
            # Re-read happens inside each engine stage, after the lock is acquired.
            return operations[stage](goal_id)

    state = await _run_with_heartbeat(stage, _locked_stage)
    return {"goal_id": goal_id, "state": state}


@workflow.defn
class DurableGoalWorkflow:
    """Temporal wrapper around the existing deterministic goal state machine.

    Intake is keyed by a stable request id, every long-running activity heartbeats,
    and PostgreSQL-backed deployments acquire a per-goal advisory lock before a
    stage executes. Activity retries therefore resume instead of duplicating work.
    """

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=30),
            maximum_attempts=3,
        )
        common = {
            "start_to_close_timeout": timedelta(hours=2),
            "heartbeat_timeout": timedelta(minutes=1),
            "retry_policy": retry,
        }

        intake = await workflow.execute_activity(
            run_pipeline_stage,
            {"stage": "intake", "request": request},
            **common,
        )
        goal_id = intake["goal_id"]

        for stage in ("plan", "plan_review", "complexity", "build", "final_review", "apply"):
            await workflow.execute_activity(
                run_pipeline_stage,
                {"stage": stage, "request": request, "goal_id": goal_id},
                **common,
            )

        return await workflow.execute_activity(
            run_pipeline_stage,
            {"stage": "finish", "request": request, "goal_id": goal_id},
            **common,
        )


async def run_temporal_pipeline(request: dict[str, Any]) -> dict[str, Any]:
    import uuid
    from temporalio.client import Client

    require_temporal_postgres()
    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "agent-army")
    client = await Client.connect(address, namespace=namespace)

    durable_request = dict(request)
    request_id = str(
        durable_request.get("_request_id")
        or os.getenv("AGENT_ARMY_REQUEST_ID", "").strip()
        or uuid.uuid4().hex
    )
    durable_request["_request_id"] = request_id
    workflow_id = durable_request.get("goal_id") or f"agent-army-{request_id}"

    return await client.execute_workflow(
        DurableGoalWorkflow.run,
        durable_request,
        id=workflow_id,
        task_queue=task_queue,
    )
