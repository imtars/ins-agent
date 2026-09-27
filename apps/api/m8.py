"""Local M8 enqueue and artifact-bound review API; workers run the graph."""

from contextlib import asynccontextmanager
import secrets
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from packages.agent.m8_graph import build_m8_workflow
from packages.persistence.approvals import PublishGuardViolation, psycopg_url
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.jobs import JobQueue
from packages.persistence.reviews import ReviewStore


class M8Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="M8_", env_file=".env", extra="ignore")
    database_url: str
    reader_database_url: str
    review_token: str = Field(min_length=20)
    reviewer_id: str = Field(min_length=1)


class CreateRun(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class ReviewRun(BaseModel):
    decision: Literal["approve", "reject"]
    artifact_id: str
    artifact_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rerun_targets: list[Literal["sql", "rag", "synthesis"]] = Field(default_factory=list)
    comment: str = Field(default="", max_length=2000)


def config(run_id: str) -> dict:
    try:
        UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return {"configurable": {"thread_id": run_id}}


def create_app(settings: M8Settings | None = None) -> FastAPI:
    settings = settings or M8Settings()
    primary, reader = make_url(settings.database_url), make_url(settings.reader_database_url)
    if primary.database == reader.database and primary.host in {reader.host, "localhost", "127.0.0.1"}:
        raise ValueError("M8 job/checkpoint database must differ from business database")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        queue = JobQueue(settings.database_url)
        artifacts = ArtifactStore(settings.database_url)
        reviews = ReviewStore(settings.database_url, artifacts)
        async with AsyncPostgresSaver.from_conn_string(
                psycopg_url(settings.database_url),
                serde=checkpoint_serializer()) as saver:
            await saver.setup()
            await queue.setup()
            await artifacts.setup()
            await reviews.setup()

            async def no_lease():
                return None

            # API only inspects checkpoints; worker owns all node execution.
            graph = build_m8_workflow(None, None, artifacts, reviews,
                checkpointer=saver, assert_lease=no_lease)
            app.state.queue = queue
            app.state.artifacts = artifacts
            app.state.reviews = reviews
            app.state.graph = graph
            yield

    app = FastAPI(title="M8 local job and review API", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    from apps.api.run_control import RunControl

    @app.post("/runs", status_code=202)
    async def create_run(request: CreateRun):
        return await RunControl(app.state.queue, app.state.graph, app.state.reviews).create(request)

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str):
        return await RunControl(app.state.queue, app.state.graph, app.state.reviews).get(run_id)

    @app.post("/runs/{run_id}/review", status_code=202)
    async def review_run(run_id: str, request: ReviewRun,
                         x_review_token: str | None = Header(default=None)):
        if x_review_token is None or not secrets.compare_digest(
                x_review_token, settings.review_token):
            raise HTTPException(status_code=403, detail="reviewer authorization required")
        return await RunControl(app.state.queue, app.state.graph, app.state.reviews).review(
            run_id, request, settings.reviewer_id)

    return app


def app_from_env():
    return create_app()
