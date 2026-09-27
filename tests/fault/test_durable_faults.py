"""Injected failures cross the real PostgreSQL checkpoint/lease/artifact boundary."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
import pytest
from psycopg import AsyncConnection, errors
from pydantic import TypeAdapter

from apps.runner.worker import run_claimed
from packages.agent.faults import (FaultInjector, FaultTolerantModel,
                                    FaultTolerantTools, RetryPolicy)
from packages.agent.m8_graph import build_m8_workflow
from packages.agent.models import AnalysisResult, VerificationResult
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.jobs import JobQueue, LeaseLost
from packages.persistence.reviews import ReviewStore
from tests.agent.test_m5_graph import StubModel, StubTools


URL = os.getenv("M9_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="M9_TEST_DATABASE_URL required")


async def stores():
    queue, artifacts = JobQueue(URL), ArtifactStore(URL)
    reviews = ReviewStore(URL, artifacts)
    await queue.setup()
    await artifacts.setup()
    await reviews.setup()
    # The M9 database is dedicated to this fault suite.
    async with await AsyncConnection.connect(URL) as conn:
        await conn.execute("""
            UPDATE agent_jobs SET status='FAILED', locked_by=NULL,
                lock_token=NULL, locked_until=NULL, last_error='test isolation'
            WHERE status IN ('PENDING','RUNNING')
        """)
    return queue, artifacts, reviews


@pytest.mark.parametrize("case,stage", [
    ("llm_timeout", "planner"),
    ("postgres_timeout", "data_describe_schema"),
    ("milvus_timeout", "knowledge_search_knowledge"),
    ("mcp_failure", "data_describe_schema"),
])
def test_one_transient_fault_recovers_without_duplicate_artifacts(case, stage):
    async def scenario():
        queue, artifacts, reviews = await stores()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_both"})
        job = await queue.claim(f"fault-{case}", 30)
        policy = RetryPolicy(base_delay_seconds=0)
        injector = FaultInjector(case=case, stage=stage, enabled=True)
        model = FaultTolerantModel(StubModel(), policy, injector)
        tools = FaultTolerantTools(StubTools(), policy, injector)
        async with AsyncPostgresSaver.from_conn_string(
                URL, serde=checkpoint_serializer()) as saver:
            await saver.setup()

            async def lease():
                await queue.assert_lease(job)

            graph = build_m8_workflow(model, tools, artifacts, reviews,
                                       checkpointer=saver, assert_lease=lease)
            result = await run_claimed(job, queue, graph, reviews)
            state = await graph.aget_state({"configurable": {"thread_id": run_id}})
            assert result["status"] == "WAITING_APPROVAL"
            assert state.values["status"] == "PASS"
            assert injector.fired == 1 and len(policy.events) == 1
            assert state.values["trace"].count("planner") == 1
            assert state.values["trace"].count("synthesis") == 1
            assert state.values["refs"]["sql"].version == 1
            assert state.values["refs"]["rag"].version == 1
            assert await reviews.get(run_id, 1) is None
            async with await AsyncConnection.connect(URL) as conn:
                counts = await (await conn.execute("""
                    SELECT stage, count(*) FROM run_artifacts
                    WHERE run_id=%s GROUP BY stage
                """, (run_id,))).fetchall()
            assert dict(counts) == {"sql": 1, "rag": 1,
                                    "analysis": 1, "verification": 1}
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["verifier_failure", "llm_invalid_json"])
def test_verifier_failure_blocks_review_and_publication(case):
    async def scenario():
        queue, artifacts, reviews = await stores()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_rag"})
        job = await queue.claim(f"fault-{case}", 30)
        policy = RetryPolicy(base_delay_seconds=0)
        injector = FaultInjector(case=case, stage="verifier", enabled=True)
        model = FaultTolerantModel(StubModel(), policy, injector)
        tools = FaultTolerantTools(StubTools(), policy, injector)
        async with AsyncPostgresSaver.from_conn_string(
                URL, serde=checkpoint_serializer()) as saver:
            await saver.setup()

            async def lease():
                await queue.assert_lease(job)

            graph = build_m8_workflow(model, tools, artifacts, reviews,
                                       checkpointer=saver, assert_lease=lease)
            result = await run_claimed(job, queue, graph, reviews)
            state = await graph.aget_state({"configurable": {"thread_id": run_id}})
            assert result["status"] == "COMPLETED"
            assert state.values["status"] == "BLOCK" and not state.next
            assert "human_review" not in state.values["trace"]
            assert injector.fired == 1 and not policy.events
            assert await reviews.get(run_id, 1) is None
            assert not state.values.get("publication")
            async with await AsyncConnection.connect(URL) as conn:
                count = (await (await conn.execute("""
                    SELECT count(*) FROM m8_publications WHERE run_id=%s
                """, (run_id,))).fetchone())[0]
            assert count == 0
    asyncio.run(scenario())


def test_actual_postgres_statement_timeout_is_not_a_fabricated_result():
    async def scenario():
        queue, _, _ = await stores()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_sql"})
        async with await AsyncConnection.connect(URL) as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '20ms'")
                with pytest.raises(errors.QueryCanceled):
                    await conn.execute("SELECT pg_sleep(0.1)")
        job = await queue.claim("after-timeout", 30)
        assert str(job["run_id"]) == run_id and job["attempt"] == 1
        await queue.finish(job, "FAILED", "actual PostgreSQL timeout test")
    asyncio.run(scenario())


def test_old_and_new_workers_cannot_write_checkpoints_concurrently():
    async def scenario():
        queue, _, _ = await stores()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_sql"})
        first = await queue.claim("first", 1)
        entered = asyncio.Event()

        async def second_writer(second):
            async with queue.execution_lock(second, 5):
                entered.set()

        async with queue.execution_lock(first, 1):
            await asyncio.sleep(1.2)
            second = await queue.claim("second", 5)
            assert second["id"] == first["id"] and second["attempt"] == 2
            task = asyncio.create_task(second_writer(second))
            await asyncio.sleep(0.4)
            assert not entered.is_set()
        await asyncio.wait_for(task, 5)
        assert entered.is_set()
        with pytest.raises(LeaseLost):
            await queue.finish(first, "COMPLETED")
        await queue.finish(second, "COMPLETED")
    asyncio.run(scenario())


def test_runner_process_crash_resumes_original_thread_from_checkpoint():
    async def scenario():
        queue, artifacts, reviews = await stores()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_both"})
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.fault.runner_child", URL, run_id,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _, stderr = await child.communicate()
        assert child.returncode == 73, stderr.decode()[-1500:]
        await asyncio.sleep(1.2)
        second = await queue.claim("crash-replacement", 30)
        assert str(second["run_id"]) == run_id and second["attempt"] == 2
        model, tools = StubModel(), StubTools()
        async with AsyncPostgresSaver.from_conn_string(
                URL, serde=checkpoint_serializer()) as saver:
            await saver.setup()

            async def lease():
                await queue.assert_lease(second)

            graph = build_m8_workflow(model, tools, artifacts, reviews,
                                       checkpointer=saver, assert_lease=lease)
            before = await graph.aget_state({"configurable": {"thread_id": run_id}})
            assert before.values["trace"] == ["planner"]
            assert before.next == ("fork",)
            result = await run_claimed(second, queue, graph, reviews)
            assert result["status"] == "WAITING_APPROVAL"
            after = await graph.aget_state({"configurable": {"thread_id": run_id}})
            assert after.values["trace"].count("planner") == 1
            assert model.calls.count("planner") == 0
            assert after.values["refs"]["sql"].version == 1
            assert after.values["refs"]["rag"].version == 1
    asyncio.run(scenario())


def test_replayed_publish_returns_one_receipt_for_same_approved_content():
    async def scenario():
        queue, artifacts, reviews = await stores()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_sql"})
        job = await queue.claim("publish-replay", 30)

        async def lease():
            await queue.assert_lease(job)

        async def analysis():
            return AnalysisResult(summary="Synthetic report", claims=[])

        ref, _ = await artifacts.load_or_compute(run_id, "analysis", 1,
            TypeAdapter(AnalysisResult), analysis, assert_lease=lease)
        decision = await reviews.decide(run_id, 1, ref, status="APPROVED",
                                         reviewer_id="fault-suite-reviewer")
        verdict = VerificationResult(status="PASS", issues=[])
        first = await reviews.publish(run_id, decision, ref, verdict)
        second = await reviews.publish(run_id, decision, ref, verdict)
        assert first == second
        async with await AsyncConnection.connect(URL) as conn:
            count = (await (await conn.execute(
                "SELECT count(*) FROM m8_publications WHERE run_id=%s",
                (run_id,))).fetchone())[0]
        assert count == 1
        await queue.finish(job, "COMPLETED")
    asyncio.run(scenario())


def test_process_crash_after_artifact_write_reuses_same_generation():
    async def scenario():
        queue, artifacts, _ = await stores()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_sql"})
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.fault.artifact_child", URL, run_id,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _, stderr = await child.communicate()
        assert child.returncode == 74, stderr.decode()[-1500:]
        await asyncio.sleep(1.2)
        replacement = await queue.claim("artifact-replacement", 30)
        assert str(replacement["run_id"]) == run_id and replacement["attempt"] == 2

        async def lease():
            await queue.assert_lease(replacement)

        async def must_not_recompute():
            raise AssertionError("persisted stage was recomputed after crash")

        ref, value = await artifacts.load_or_compute(run_id, "analysis", 1,
            TypeAdapter(AnalysisResult), must_not_recompute, assert_lease=lease)
        assert ref.version == 1 and value.summary == "computed once before crash"
        async with await AsyncConnection.connect(URL) as conn:
            count = (await (await conn.execute("""
                SELECT count(*) FROM run_artifacts
                WHERE run_id=%s AND stage='analysis'
            """, (run_id,))).fetchone())[0]
        assert count == 1
        await queue.finish(replacement, "COMPLETED")
    asyncio.run(scenario())
