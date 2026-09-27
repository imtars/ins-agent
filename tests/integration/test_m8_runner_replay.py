"""M8 PostgreSQL lease recovery, immutable artifacts, and targeted graph replay."""

import asyncio
import os
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
import pytest
from psycopg import AsyncConnection
from pydantic import TypeAdapter

from apps.runner.worker import run_claimed
from packages.agent.m8_graph import build_m8_workflow
from packages.agent.models import AnalysisResult, VerificationResult
from packages.persistence.approvals import PublishGuardViolation
from packages.persistence.artifacts import ArtifactConflict, ArtifactStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.jobs import JobQueue, LeaseLost
from packages.persistence.reviews import ReviewStore
from tests.agent.test_m5_graph import StubModel, StubTools


URL = os.getenv("M8_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="M8_TEST_DATABASE_URL required")


async def isolate_queue():
    # Dedicated integration database; previous interrupted test runs may leave leases.
    async with await AsyncConnection.connect(URL) as conn:
        await conn.execute("""
            UPDATE agent_jobs SET status = 'FAILED', locked_by = NULL,
                lock_token = NULL, locked_until = NULL, last_error = 'test isolation'
            WHERE status IN ('PENDING', 'RUNNING')
        """)


def test_expired_lease_reclaims_same_run_and_rejects_stale_worker():
    async def scenario():
        queue = JobQueue(URL)
        await queue.setup()
        await isolate_queue()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_both"})
        first = await queue.claim("worker-one", 1)
        assert str(first["run_id"]) == run_id and first["attempt"] == 1
        await asyncio.sleep(1.2)
        second = await queue.claim("worker-two", 5)
        assert second["id"] == first["id"]
        assert second["attempt"] == 2
        assert str(second["run_id"]) == run_id
        with pytest.raises(LeaseLost):
            await queue.finish(first, "COMPLETED")
        await queue.finish(second, "COMPLETED")
    asyncio.run(scenario())


def test_artifact_stage_retry_is_idempotent_and_detects_tampering():
    async def scenario():
        queue = JobQueue(URL)
        artifacts = ArtifactStore(URL)
        reviews = ReviewStore(URL, artifacts)
        await queue.setup()
        await artifacts.setup()
        await reviews.setup()
        await isolate_queue()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_rag"})
        job = await queue.claim("artifact-worker", 30)
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            return AnalysisResult(summary="first", claims=[])

        async def lease():
            await queue.assert_lease(job)

        adapter = TypeAdapter(AnalysisResult)
        first, _ = await artifacts.load_or_compute(run_id, "analysis", 1,
            adapter, compute, assert_lease=lease)
        again, _ = await artifacts.load_or_compute(run_id, "analysis", 1,
            adapter, compute, assert_lease=lease)
        assert first == again and calls == 1
        decision = await reviews.decide(run_id, 1, first, status="APPROVED",
                                         reviewer_id="reviewer")
        async with await AsyncConnection.connect(URL) as conn:
            await conn.execute("""
                UPDATE run_artifacts SET content_json = '{"summary":"tampered","claims":[]}'
                WHERE artifact_id = %s
            """, (first.artifact_id,))
        with pytest.raises(ArtifactConflict, match="hash mismatch"):
            await artifacts.load(first, adapter)
        with pytest.raises(PublishGuardViolation, match="analysis changed"):
            await reviews.publish(run_id, decision, first,
                VerificationResult(status="PASS", issues=[]))
        await queue.finish(job, "FAILED", "test tamper completed")
    asyncio.run(scenario())


def test_replay_rag_only_preserves_sql_reference_and_binds_approval():
    async def scenario():
        queue = JobQueue(URL)
        artifacts = ArtifactStore(URL)
        reviews = ReviewStore(URL, artifacts)
        await queue.setup()
        await artifacts.setup()
        await reviews.setup()
        await isolate_queue()
        run_id = str(uuid4())
        await queue.enqueue(run_id, "START", {"query": "route_both"})
        first = await queue.claim("first", 30)
        model, tools = StubModel(), StubTools()
        async with AsyncPostgresSaver.from_conn_string(
                URL, serde=checkpoint_serializer()) as saver:
            await saver.setup()

            async def lease():
                await queue.assert_lease(first)

            graph = build_m8_workflow(model, tools, artifacts, reviews,
                                       checkpointer=saver, assert_lease=lease)
            result = await run_claimed(first, queue, graph, reviews)
            assert result["status"] == "WAITING_APPROVAL"
            config = {"configurable": {"thread_id": run_id}}
            paused = await graph.aget_state(config)
            assert paused.values["status"] == "PASS"
            assert paused.values["trace"].count("synthesis") == 1
            sql_first = paused.values["refs"]["sql"]
            rag_first = paused.values["refs"]["rag"]
            analysis_first = paused.values["refs"]["analysis"]
            assert sql_first.version == rag_first.version == 1
            tool_calls = list(tools.calls)

            rejection = await reviews.decide(run_id, 1, analysis_first,
                status="REJECTED", reviewer_id="reviewer", revision_targets=["rag"])
            await queue.enqueue(run_id, "RESUME", {"approval_id": rejection.approval_id})
            second = await queue.claim("second", 30)

            async def second_lease():
                await queue.assert_lease(second)

            graph2 = build_m8_workflow(model, tools, artifacts, reviews,
                                        checkpointer=saver, assert_lease=second_lease)
            replay = await run_claimed(second, queue, graph2, reviews)
            assert replay["status"] == "WAITING_APPROVAL"
            revised = await graph2.aget_state(config)
            refs = revised.values["refs"]
            assert revised.values["cycle"] == 2
            assert refs["sql"] == sql_first
            assert refs["rag"].version == 2 and refs["rag"] != rag_first
            assert refs["analysis"].version == 2
            assert refs["verification"].version == 2
            assert tools.calls.count("data_execute_readonly_query") == 1
            assert tools.calls.count("knowledge_search_knowledge") == 2
            assert model.calls.count("planner") == 1
            assert revised.values["trace"].count("synthesis") == 2
            assert revised.values["trace"].count("verifier") == 2
            with pytest.raises(PublishGuardViolation, match="current analysis"):
                await reviews.decide(run_id, 1, analysis_first,
                    status="APPROVED", reviewer_id="reviewer")

            approved = await reviews.decide(run_id, 2, refs["analysis"],
                status="APPROVED", reviewer_id="reviewer")
            with pytest.raises(PublishGuardViolation, match="approved artifact"):
                await reviews.publish(run_id, approved, analysis_first,
                    VerificationResult(status="PASS", issues=[]))
            await queue.enqueue(run_id, "RESUME", {"approval_id": approved.approval_id})
            third = await queue.claim("third", 30)

            async def third_lease():
                await queue.assert_lease(third)

            graph3 = build_m8_workflow(model, tools, artifacts, reviews,
                                        checkpointer=saver, assert_lease=third_lease)
            published = await run_claimed(third, queue, graph3, reviews)
            assert published["status"] == "COMPLETED"
            final = await graph3.aget_state(config)
            assert final.values["status"] == "PUBLISHED"
            assert final.values["publication"]["artifact_id"] == refs["analysis"].artifact_id
            assert final.values["publication"]["content_hash"] == refs["analysis"].content_hash
            assert final.values["trace"].count("publish") == 1
    asyncio.run(scenario())
