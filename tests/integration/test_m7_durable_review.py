"""Real PostgreSQL checkpoints and local API review across app lifespans."""

import asyncio
import os
from uuid import uuid4

import httpx
from langgraph.types import Command
import pytest

from apps.api.m7 import M7Settings, create_app
from packages.agent.contracts import (ContractViolation, DURABLE_NODE_CONTRACTS,
                                     NodeContract, validate_registry)
from packages.agent.models import AnalysisResult, ApprovalResult, VerificationResult
from packages.persistence.approvals import ApprovalStore, PublishGuardViolation
from tests.agent.test_m5_graph import StubModel, StubTools


M7_DATABASE_URL = os.environ.get("M7_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not M7_DATABASE_URL,
                                reason="M7_TEST_DATABASE_URL is needed for Postgres checkpoints")


def settings() -> M7Settings:
    return M7Settings(checkpoint_database_url=M7_DATABASE_URL,
                      reader_database_url="postgresql+asyncpg://reader@localhost/other_db",
                      review_token="m7-test-review-token-long-enough",
                      reviewer_id="local_reviewer")


def test_m7_rejects_checkpoint_tables_in_business_database():
    values = settings().model_copy(update={
        "reader_database_url": M7_DATABASE_URL.replace(
            "postgresql://", "postgresql+asyncpg://").replace(
            "127.0.0.1", "localhost")})
    with pytest.raises(ValueError, match="must be separate"):
        create_app(values, test_model=StubModel(), test_tools=StubTools())


def test_publish_registry_requires_human_review_only(monkeypatch):
    main = {"planner", "fork", "sql_only", "rag_only", "sql_parallel",
            "rag_parallel", "synthesis", "verifier", "human_review", "publish"}
    subgraphs = {"sql_subgraph.data_analyst", "rag_subgraph.knowledge_researcher"}
    validate_registry(main, subgraphs, durable=True)
    original = DURABLE_NODE_CONTRACTS["publish"]
    monkeypatch.setitem(DURABLE_NODE_CONTRACTS, "publish", NodeContract(
        "publish", original.input_model, original.output_model,
        ("human_review", "verifier")))
    with pytest.raises(ContractViolation, match="only predecessor"):
        validate_registry(main, subgraphs, durable=True)


async def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


def test_postgres_interrupt_restart_resume_and_publish_guard():
    async def scenario():
        model = StubModel()
        first = create_app(settings(), test_model=model, test_tools=StubTools())
        async with first.router.lifespan_context(first):
            with pytest.raises(ContractViolation, match="run_id must equal"):
                await first.state.workflow.ainvoke(
                    {"run_id": str(uuid4()), "user_query": "route_rag", "trace": []},
                    {"configurable": {"thread_id": str(uuid4())}})
            async with await client_for(first) as client:
                created = await client.post("/runs", json={"query": "route_rag"})
                assert created.status_code == 201, created.text
                run_id = created.json()["run_id"]
                assert created.json()["thread_id"] == run_id
                assert created.json()["status"] == "WAITING_APPROVAL"
                waiting = (await client.get(f"/runs/{run_id}")).json()
                assert waiting["next"] == ["human_review"]
                assert waiting["verification"]["status"] == "PASS"
                assert "publish" not in waiting["trace"]
                denied = await client.post(f"/runs/{run_id}/review",
                                           json={"decision": "approve"})
                assert denied.status_code == 403
                assert await first.state.approval_store.get_decision(run_id) is None
                with pytest.raises(PublishGuardViolation, match="stored reviewer"):
                    await first.state.approval_store.publish(
                        run_id=run_id,
                        approval=ApprovalResult(run_id=run_id,
                            approval_id=str(uuid4()), status="APPROVED",
                            reviewer_id="forged"),
                        verification=VerificationResult(status="PASS", issues=[]),
                        analysis=AnalysisResult(summary="forged", claims=[]))
                assert await first.state.approval_store.get_publication(run_id) is None

        # A new app lifespan opens a new AsyncPostgresSaver connection and graph instance.
        second_model = StubModel()
        second = create_app(settings(), test_model=second_model, test_tools=StubTools())
        async with second.router.lifespan_context(second):
            async with await client_for(second) as client:
                waiting = (await client.get(f"/runs/{run_id}")).json()
                assert waiting["status"] == "WAITING_APPROVAL"
                assert second_model.calls == []
                approved = await client.post(
                    f"/runs/{run_id}/review", json={"decision": "approve"},
                    headers={"X-Review-Token": settings().review_token})
                assert approved.status_code == 200, approved.text
                assert approved.json()["status"] == "PUBLISHED"
                published = (await client.get(f"/runs/{run_id}")).json()
                assert published["next"] == []
                assert published["trace"].count("human_review") == 1
                assert published["trace"].count("publish") == 1
                assert published["publication"]["run_id"] == run_id
                assert published["approval"]["reviewer_id"] == "local_reviewer"
                assert second_model.calls == []
                assert (await client.post(f"/runs/{run_id}/review",
                    json={"decision": "approve"},
                    headers={"X-Review-Token": settings().review_token})).status_code == 409
                receipt = await second.state.approval_store.get_publication(run_id)
                assert receipt.content_hash == published["publication"]["content_hash"]
                state = await second.state.workflow.aget_state(
                    {"configurable": {"thread_id": run_id}})
                values = state.values
                again = await second.state.approval_store.publish(
                    run_id=run_id, approval=values["approval"],
                    verification=values["verification"], analysis=values["analysis"])
                assert again == receipt
                with pytest.raises(PublishGuardViolation, match="stored reviewer"):
                    await second.state.approval_store.publish(
                        run_id=run_id,
                        approval=values["approval"].model_copy(
                            update={"reviewer_id": "forged"}),
                        verification=values["verification"],
                        analysis=values["analysis"])
                with pytest.raises(PublishGuardViolation, match="requires PASS"):
                    await second.state.approval_store.publish(
                        run_id=run_id, approval=values["approval"],
                        verification=VerificationResult(status="BLOCK", issues=["test"]),
                        analysis=values["analysis"])

                rejected = await client.post("/runs", json={"query": "route_rag"})
                reject_id = rejected.json()["run_id"]
                response = await client.post(
                    f"/runs/{reject_id}/review",
                    json={"decision": "reject", "comment": "needs revision"},
                    headers={"X-Review-Token": settings().review_token})
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "REJECTED"
                assert await second.state.approval_store.get_publication(reject_id) is None

                forged = await client.post("/runs", json={"query": "route_rag"})
                forged_id = forged.json()["run_id"]
                with pytest.raises(PublishGuardViolation, match="stored review"):
                    await second.state.workflow.ainvoke(
                        Command(resume={"approval_id": str(uuid4())}),
                        {"configurable": {"thread_id": forged_id}})
                assert await second.state.approval_store.get_publication(forged_id) is None

        blocked = create_app(settings(), test_model=StubModel(verifier_fails=True),
                             test_tools=StubTools())
        async with blocked.router.lifespan_context(blocked):
            async with await client_for(blocked) as client:
                response = await client.post("/runs", json={"query": "route_rag"})
                assert response.status_code == 201
                assert response.json()["status"] == "BLOCK"
                blocked_id = response.json()["run_id"]
                state = (await client.get(f"/runs/{blocked_id}")).json()
                assert state["next"] == []
                assert "human_review" not in state["trace"]
                assert await blocked.state.approval_store.get_decision(blocked_id) is None
    asyncio.run(scenario())
