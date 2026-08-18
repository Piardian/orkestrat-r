from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from orchestration.temporal_flow import DurableGoalWorkflow, run_pipeline_stage
from observability import init_sentry


async def main() -> None:
    init_sentry()
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    worker = Worker(
        client,
        task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "agent-army"),
        workflows=[DurableGoalWorkflow],
        activities=[run_pipeline_stage],
    )
    print(f"Temporal worker ready: {os.getenv('TEMPORAL_TASK_QUEUE', 'agent-army')}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
