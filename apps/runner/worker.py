"""Claim M8 jobs, renew leases, and resume their original LangGraph threads."""

import argparse
import asyncio
import os
import socket
from contextlib import suppress

from fastmcp import Client, ClientGroup
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import create_async_engine

from packages.agent.m8_graph import build_m8_workflow
from packages.llm.client import select_chat_client
from packages.persistence.approvals import psycopg_url
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.jobs import JobQueue
from packages.persistence.reviews import ReviewStore
from services.mcp_data.server import create_server as data_server
from services.mcp_knowledge.server import build_real_server


async def run_claimed(job: dict, queue: JobQueue, graph, reviews: ReviewStore, *,
                      lease_seconds: int = 30,
                      crash_after_planner: bool = False) -> dict:
    config = {"configurable": {"thread_id": str(job["run_id"])}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        if job["kind"] != "START":
            raise ValueError("resume job has no checkpoint")
        graph_input = {"run_id": str(job["run_id"]),
                       "user_query": job["payload"]["query"],
                       "refs": {}, "trace": []}
    elif snapshot.next == ("human_review",) and any(
            task.interrupts for task in snapshot.tasks):
        current = await reviews.get(str(job["run_id"]), snapshot.values["cycle"])
        if (job["kind"] == "RESUME" and current is not None
                and current.approval_id == job["payload"]["approval_id"]):
            graph_input = Command(resume={"approval_id": job["payload"]["approval_id"]})
        else:
            await queue.finish(job, "WAITING_APPROVAL")
            return {"status": "WAITING_APPROVAL"}
    elif snapshot.next:
        graph_input = None
    else:
        await queue.finish(job, "COMPLETED")
        return {"status": snapshot.values.get("status", "COMPLETED")}

    async def heartbeat():
        while True:
            await asyncio.sleep(max(0.2, lease_seconds / 3))
            await queue.renew(job, lease_seconds)

    task = asyncio.create_task(heartbeat())
    try:
        async for update in graph.astream(graph_input, config,
                                          stream_mode="updates", durability="sync"):
            await queue.assert_lease(job)
            if crash_after_planner and "planner" in update:
                # Acceptance-only failpoint: the synchronous checkpoint is durable.
                os._exit(73)
        snapshot = await graph.aget_state(config)
        waiting = (snapshot.next == ("human_review",)
                   and any(item.interrupts for item in snapshot.tasks))
        status = "WAITING_APPROVAL" if waiting else "COMPLETED"
        await queue.finish(job, status)
        return {"status": status, "snapshot": snapshot}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def work_once(database_url: str, reader_url: str, milvus_uri: str,
                    *, provider: str = "deepseek", lease_seconds: int = 30,
                    crash_after_planner: bool = False, worker_id: str | None = None):
    queue = JobQueue(database_url)
    artifacts = ArtifactStore(database_url)
    reviews = ReviewStore(database_url, artifacts)
    await queue.setup()
    await artifacts.setup()
    await reviews.setup()
    engine = create_async_engine(reader_url)
    try:
        async with AsyncPostgresSaver.from_conn_string(
                psycopg_url(database_url), serde=checkpoint_serializer()) as saver:
            await saver.setup()
            async with ClientGroup({"data": Client(data_server(engine)),
                    "knowledge": Client(build_real_server(milvus_uri))}) as tools:
                model = select_chat_client(provider)
                job = await queue.claim(worker_id or f"{socket.gethostname()}:{os.getpid()}",
                                        lease_seconds)
                if job is None:
                    return None

                async def assert_lease():
                    await queue.assert_lease(job)

                graph = build_m8_workflow(model, tools, artifacts, reviews,
                    checkpointer=saver, assert_lease=assert_lease)
                try:
                    result = await run_claimed(job, queue, graph, reviews,
                        lease_seconds=lease_seconds,
                        crash_after_planner=crash_after_planner)
                    print({"run_id": str(job["run_id"]), "attempt": job["attempt"],
                           "status": result["status"],
                           "model_response_ids": getattr(model, "response_models", [])},
                          flush=True)
                    return result
                except Exception as exc:
                    # A lost lease belongs to the next worker; all other failures stay visible.
                    try:
                        await queue.finish(job, "FAILED", f"{type(exc).__name__}: {exc}"[:1000])
                    except Exception:
                        pass
                    raise
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("M8_DATABASE_URL"))
    parser.add_argument("--reader-url", default=os.getenv("M8_READER_DATABASE_URL"))
    parser.add_argument("--milvus-uri", default=os.getenv("M8_MILVUS_URI",
                                                  "http://127.0.0.1:19530"))
    parser.add_argument("--provider", default=os.getenv("M8_PROVIDER", "deepseek"))
    parser.add_argument("--lease-seconds", type=int, default=10)
    parser.add_argument("--crash-after-planner", action="store_true")
    args = parser.parse_args()
    if not args.database_url or not args.reader_url:
        parser.error("database and reader URLs are required")
    asyncio.run(work_once(args.database_url, args.reader_url, args.milvus_uri,
        provider=args.provider, lease_seconds=args.lease_seconds,
        crash_after_planner=args.crash_after_planner))


if __name__ == "__main__":
    main()
