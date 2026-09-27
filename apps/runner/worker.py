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
from packages.agent.faults import (FaultInjector, FaultTolerantModel,
                                   FaultTolerantTools, RetryPolicy)
from packages.llm.client import select_chat_client
from packages.persistence.approvals import psycopg_url
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.jobs import JobQueue
from packages.persistence.events import EventStore
from packages.persistence.reviews import ReviewStore
from services.mcp_data.server import create_server as data_server
from services.mcp_knowledge.server import build_real_server


async def run_claimed(job: dict, queue: JobQueue, graph, reviews: ReviewStore, *,
                      lease_seconds: int = 30,
                      crash_after_planner: bool = False,
                      events: EventStore | None = None, policy: RetryPolicy | None = None) -> dict:
    async with queue.execution_lock(job, lease_seconds):
        return await _run_claimed_locked(job, queue, graph, reviews,
            lease_seconds=lease_seconds, crash_after_planner=crash_after_planner,
            events=events, policy=policy)


async def _run_claimed_locked(job: dict, queue: JobQueue, graph, reviews: ReviewStore, *,
                              lease_seconds: int, crash_after_planner: bool,
                              events: EventStore | None = None,
                              policy: RetryPolicy | None = None) -> dict:
    async def emit(kind: str, *, node: str | None = None, payload: dict | None = None):
        if events:
            await events.append(str(job["run_id"]), kind, job_id=str(job["id"]),
                                attempt=job["attempt"], node=node, payload=payload)

    retry_cursor = 0

    async def flush_retries():
        nonlocal retry_cursor
        if policy:
            for item in policy.events[retry_cursor:]:
                await emit("dependency.retry", payload=item.__dict__)
            retry_cursor = len(policy.events)

    await emit("workflow.resumed" if job["kind"] == "RESUME" else "workflow.started",
               payload={"job_kind": job["kind"]})
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
            await emit("workflow.interrupted")
            return {"status": "WAITING_APPROVAL"}
    elif snapshot.next:
        graph_input = None
    else:
        await queue.finish(job, "COMPLETED")
        await emit("workflow.completed", payload={"status": snapshot.values.get("status")})
        return {"status": snapshot.values.get("status", "COMPLETED")}

    async def heartbeat():
        while True:
            await asyncio.sleep(max(0.2, lease_seconds / 3))
            await queue.renew(job, lease_seconds)

    task = asyncio.create_task(heartbeat())
    try:
        async for mode, update in graph.astream(graph_input, config,
                                                stream_mode=["tasks", "updates"],
                                                durability="sync"):
            await queue.assert_lease(job)
            await flush_retries()
            if mode == "tasks":
                if "input" in update:
                    node = update["name"]
                    label = "sql" if node.startswith("sql") else (
                        "rag" if node.startswith("rag") else
                        "verification" if node == "verifier" else node)
                    await emit(f"{label}.started", node=node)
                continue
            for node, value in update.items():
                if node == "__interrupt__":
                    continue
                await emit("verification.completed" if node == "verifier" else
                           "synthesis.completed" if node == "synthesis" else
                           f"{('sql' if node.startswith('sql') else 'rag' if node.startswith('rag') else node)}.completed",
                           node=node,
                           payload={"status": value.get("status") if isinstance(value, dict) else None})
            if crash_after_planner and "planner" in update:
                # Acceptance-only failpoint: the synchronous checkpoint is durable.
                os._exit(73)
        snapshot = await graph.aget_state(config)
        waiting = (snapshot.next == ("human_review",)
                   and any(item.interrupts for item in snapshot.tasks))
        status = "WAITING_APPROVAL" if waiting else "COMPLETED"
        await queue.finish(job, status)
        await flush_retries()
        await emit("workflow.interrupted" if waiting else "workflow.completed",
                   payload={"status": snapshot.values.get("status", status),
                            "degraded_flags": snapshot.values.get("degraded_flags", [])})
        return {"status": status, "snapshot": snapshot}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def work_once(database_url: str, reader_url: str, milvus_uri: str,
                    *, provider: str = "deepseek", lease_seconds: int = 30,
                    crash_after_planner: bool = False, worker_id: str | None = None):
    queue = JobQueue(database_url)
    injector = FaultInjector.from_env()
    policy = RetryPolicy()
    artifacts = ArtifactStore(database_url)
    reviews = ReviewStore(database_url, artifacts)
    events = EventStore(database_url)
    await queue.setup()
    await artifacts.setup()
    await reviews.setup()
    await events.setup()
    engine = create_async_engine(reader_url)
    try:
        async with AsyncPostgresSaver.from_conn_string(
                psycopg_url(database_url), serde=checkpoint_serializer()) as saver:
            await saver.setup()
            async with ClientGroup({"data": Client(data_server(engine)),
                    "knowledge": Client(build_real_server(milvus_uri,
                                                         injector=injector))}) as tools:
                model = FaultTolerantModel(select_chat_client(provider), policy, injector)
                resilient_tools = FaultTolerantTools(tools, policy, injector)
                job = await policy.run("postgres:claim", lambda: queue.claim(
                    worker_id or f"{socket.gethostname()}:{os.getpid()}", lease_seconds))
                if job is None:
                    return None

                async def assert_lease():
                    await queue.assert_lease(job)

                graph = build_m8_workflow(model, resilient_tools, artifacts, reviews,
                    checkpointer=saver, assert_lease=assert_lease)
                try:
                    result = await run_claimed(job, queue, graph, reviews,
                        lease_seconds=lease_seconds,
                        events=events, policy=policy,
                        crash_after_planner=(crash_after_planner or
                                             (injector.enabled and
                                              injector.case == "runner_crash")))
                    print({"run_id": str(job["run_id"]), "attempt": job["attempt"],
                           "status": result["status"],
                           "model_response_ids": model.response_models,
                           "fault_case": injector.case if injector.enabled else None,
                           "fault_fired": injector.fired,
                           "retry_events": [item.__dict__ for item in policy.events]},
                          flush=True)
                    return result
                except Exception as exc:
                    # A lost lease belongs to the next worker; all other failures stay visible.
                    try:
                        await queue.finish(job, "FAILED", f"{type(exc).__name__}: {exc}"[:1000])
                        await events.append(str(job["run_id"]), "workflow.failed",
                            job_id=str(job["id"]), attempt=job["attempt"],
                            payload={"error_type": type(exc).__name__,
                                     "retry_events": [item.__dict__ for item in policy.events]})
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
