"""M10 API authorization and real PostgreSQL review/publish guard integration."""

import asyncio
import os
from uuid import uuid4

import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
import pytest

from apps.api.m10 import M10Settings, create_app
from apps.runner.worker import run_claimed
from packages.agent.m8_graph import build_m8_workflow
from packages.persistence.approvals import psycopg_url
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.auth import AuthStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.events import EventStore
from packages.persistence.jobs import JobQueue
from packages.persistence.reviews import ReviewStore
from tests.agent.test_m5_graph import StubModel, StubTools


URL = os.getenv("M10_TEST_DATABASE_URL")
READER = os.getenv("M3_TEST_READER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL or not READER,
    reason="M10_TEST_DATABASE_URL and M3_TEST_READER_DATABASE_URL required")
SECRET = "m10-test-only-jwt-secret-with-at-least-32-bytes"


def test_jwt_rbac_events_artifacts_and_bound_publish():
    async def scenario():
        settings = M10Settings(database_url=URL, reader_database_url=READER,
                               jwt_secret=SECRET)
        app = create_app(settings)
        auth = AuthStore(URL, SECRET)
        await auth.setup()
        suffix = uuid4().hex[:10]
        analyst = f"analyst_{suffix}"
        reviewer = f"reviewer_{suffix}"
        admin = f"admin_{suffix}"
        password = "test-password-long-enough"
        await auth.create_user(analyst, password, "analyst")
        await auth.create_user(reviewer, password, "reviewer")
        await auth.create_user(admin, password, "admin")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                a = (await client.post("/api/auth/login", json={"username": analyst,
                      "password": password})).json()
                r = (await client.post("/api/auth/login", json={"username": reviewer,
                      "password": password})).json()
                d = (await client.post("/api/auth/login", json={"username": admin,
                      "password": password})).json()
                ah = {"Authorization": f"Bearer {a['access_token']}"}
                rh = {"Authorization": f"Bearer {r['access_token']}"}
                dh = {"Authorization": f"Bearer {d['access_token']}"}
                assert (await client.get("/api/runs")).status_code == 401
                assert (await client.post("/api/runs", json={"query": "route_rag"},
                                          headers=ah)).status_code == 202
                listing = (await client.get("/api/runs", headers=ah)).json()
                run_id = listing["runs"][0]["run_id"]
                assert (await client.post(f"/api/runs/{run_id}/review",
                         json={}, headers=ah)).status_code == 403
                assert (await client.post("/api/documents", json={"doc_id": "product_001"},
                                          headers=rh)).status_code == 403
                assert (await client.post("/api/evaluations/rag", headers=ah)).status_code == 403
                documents = (await client.get("/api/documents", headers=ah)).json()["documents"]
                assert len(documents) == 17
                assert all(doc["status"] == "draft" and doc["product_code"] is None
                           for doc in documents if doc["source_type"] == "public_consultation_draft")
                registered = await client.post("/api/documents", json={"doc_id": "product_001"},
                                               headers=dh)
                assert registered.status_code == 200
                assert registered.json()["registration"] == "verified_existing_source"
                evaluation = await client.get("/api/evaluations/rag", headers=ah)
                assert evaluation.status_code == 200
                assert evaluation.json()["status"] == "existing_report"
                rotated = await client.post("/api/auth/refresh",
                                            json={"refresh_token": a["refresh_token"]})
                assert rotated.status_code == 200
                assert (await client.post("/api/auth/refresh",
                    json={"refresh_token": a["refresh_token"]})).status_code == 401

                queue, artifacts = JobQueue(URL), ArtifactStore(URL)
                reviews, events = ReviewStore(URL, artifacts), EventStore(URL)
                job = await queue.claim("m10-test-worker", 30)
                assert str(job["run_id"]) == run_id
                async with AsyncPostgresSaver.from_conn_string(
                        psycopg_url(URL), serde=checkpoint_serializer()) as saver:
                    await saver.setup()
                    async def lease():
                        await queue.assert_lease(job)
                    graph = build_m8_workflow(StubModel(), StubTools(), artifacts,
                        reviews, checkpointer=saver, assert_lease=lease)
                    result = await run_claimed(job, queue, graph, reviews, events=events)
                    assert result["status"] == "WAITING_APPROVAL"
                state = (await client.get(f"/api/runs/{run_id}", headers=ah)).json()
                assert state["status"] == "WAITING_APPROVAL"
                refs = state["refs"]
                payload = (await client.get(f"/api/runs/{run_id}/artifacts", headers=ah)).json()
                assert "rag" in payload["artifacts"] and "verification" in payload["artifacts"]
                preview = (await client.get(f"/api/runs/{run_id}/report", headers=ah)).json()
                assert preview["published"] is False
                timeline = (await client.get(f"/api/runs/{run_id}/events", headers=ah)).json()["events"]
                assert any(item["event_type"] == "workflow.interrupted" for item in timeline)
                ref = refs["analysis"]
                body = {"decision": "approve", "artifact_id": ref["artifact_id"],
                        "artifact_version": ref["version"], "content_hash": ref["content_hash"]}
                stale = dict(body, content_hash="0" * 64)
                assert (await client.post(f"/api/runs/{run_id}/review", json=stale,
                                          headers=rh)).status_code == 409
                assert (await client.post(f"/api/runs/{run_id}/review", json=body,
                                          headers=rh)).status_code == 202
                assert (await client.post(f"/api/runs/{run_id}/review", json=body,
                                          headers=rh)).status_code == 409
                resume = await queue.claim("m10-publish-worker", 30)
                async with AsyncPostgresSaver.from_conn_string(
                        psycopg_url(URL), serde=checkpoint_serializer()) as saver:
                    await saver.setup()
                    async def second_lease():
                        await queue.assert_lease(resume)
                    graph = build_m8_workflow(StubModel(), StubTools(), artifacts,
                        reviews, checkpointer=saver, assert_lease=second_lease)
                    await run_claimed(resume, queue, graph, reviews, events=events)
                final = (await client.get(f"/api/runs/{run_id}", headers=ah)).json()
                assert final["status"] == "PUBLISHED"
                assert final["publication"]["artifact_id"] == ref["artifact_id"]
                published = (await client.get(f"/api/runs/{run_id}/report", headers=ah)).json()
                assert published["published"] is True
    asyncio.run(scenario())
