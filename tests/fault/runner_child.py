"""Subprocess failpoint used by the real PostgreSQL crash test."""

import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from apps.runner.worker import run_claimed
from packages.agent.m8_graph import build_m8_workflow
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.jobs import JobQueue
from packages.persistence.reviews import ReviewStore
from tests.agent.test_m5_graph import StubModel, StubTools


async def main(url: str, run_id: str):
    queue = JobQueue(url)
    artifacts = ArtifactStore(url)
    reviews = ReviewStore(url, artifacts)
    job = await queue.claim("crash-child", 1)
    if job is None or str(job["run_id"]) != run_id:
        raise RuntimeError("crash child claimed a different run")
    async with AsyncPostgresSaver.from_conn_string(
            url, serde=checkpoint_serializer()) as saver:
        await saver.setup()

        async def lease():
            await queue.assert_lease(job)

        graph = build_m8_workflow(StubModel(), StubTools(), artifacts, reviews,
                                   checkpointer=saver, assert_lease=lease)
        await run_claimed(job, queue, graph, reviews, lease_seconds=1,
                          crash_after_planner=True)
    raise RuntimeError("runner crash failpoint did not exit")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
