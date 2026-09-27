"""Exit after a persisted stage artifact and before any graph checkpoint."""

import asyncio
import os
import sys

from pydantic import TypeAdapter

from packages.agent.models import AnalysisResult
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.jobs import JobQueue


async def main(url: str, run_id: str):
    queue = JobQueue(url)
    artifacts = ArtifactStore(url)
    job = await queue.claim("artifact-crash-child", 1)
    if job is None or str(job["run_id"]) != run_id:
        raise RuntimeError("artifact crash child claimed a different run")

    async def lease():
        await queue.assert_lease(job)

    async def compute():
        return AnalysisResult(summary="computed once before crash", claims=[])

    await artifacts.load_or_compute(run_id, "analysis", 1,
        TypeAdapter(AnalysisResult), compute, assert_lease=lease)
    os._exit(74)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
