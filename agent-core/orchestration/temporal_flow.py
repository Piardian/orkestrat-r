from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn
async def run_pipeline_stage(payload: dict[str, Any]) -> dict[str, Any]:
    # Imports stay inside the activity so Temporal workflow sandbox remains deterministic.
    from orchestration.engine import GoalPipelineEngine, PipelineRequest

    stage = str(payload["stage"])
    request_data = dict(payload["request"])
    goal_id = payload.get("goal_id")
    if goal_id:
        request_data["goal_id"] = goal_id
        request_data["repo"] = None
        request_data["task"] = None

    engine = GoalPipelineEngine(PipelineRequest(**request_data))

    if stage == "intake":
        goal_id = engine.create_or_resume()
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

    state = operations[stage](goal_id)
    return {"goal_id": goal_id, "state": state}


@workflow.defn
class DurableGoalWorkflow:
    """Temporal wrapper around the existing idempotent goal state machine.

    Each durable activity runs one existing stage. If Temporal retries a stage,
    GoalPipelineEngine re-reads the persisted goal state and skips stages that
    already completed, preventing duplicate code application.
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
    import os
    import uuid
    from temporalio.client import Client

    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "agent-army")
    client = await Client.connect(address, namespace=namespace)
    workflow_id = request.get("goal_id") or f"agent-army-{uuid.uuid4()}"
    return await client.execute_workflow(
        DurableGoalWorkflow.run,
        request,
        id=workflow_id,
        task_queue=task_queue,
    )
