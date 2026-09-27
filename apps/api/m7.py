"""Local M7 run/review API for durable interrupt/resume acceptance."""

from contextlib import asynccontextmanager
import secrets
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException
from fastmcp import Client, ClientGroup
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from packages.agent.graph import build_workflow
from packages.llm.client import select_chat_client
from packages.persistence.approvals import ApprovalStore, psycopg_url
from packages.persistence.checkpoints import checkpoint_serializer
from services.mcp_data.server import create_server as data_server
from services.mcp_knowledge.server import build_real_server


class M7Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="M7_", env_file=".env", extra="ignore")

    checkpoint_database_url: str
    reader_database_url: str
    review_token: str = Field(min_length=20)
    reviewer_id: str = Field(min_length=1)
    milvus_uri: str = "http://127.0.0.1:19530"
    provider: Literal["proxy", "deepseek", "auto"] = "deepseek"


class CreateRun(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class ReviewRun(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=2000)


def _config(run_id: str) -> dict:
    try:
        UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return {"configurable": {"thread_id": run_id}}


def create_app(settings: M7Settings | None = None, *, test_model=None,
               test_tools=None) -> FastAPI:
    settings = settings or M7Settings()
    if (test_model is None) != (test_tools is None):
        raise ValueError("test model and tools must be provided together")
    checkpoint = make_url(settings.checkpoint_database_url)
    reader = make_url(settings.reader_database_url)
    local_aliases = {"localhost", "127.0.0.1"}
    same_host = (checkpoint.host == reader.host
                 or {checkpoint.host, reader.host} <= local_aliases)
    if (same_host and (checkpoint.port or 5432) == (reader.port or 5432)
            and checkpoint.database == reader.database):
        raise ValueError("M7 checkpoint database must be separate from the M1 business database")
    psycopg_url(settings.checkpoint_database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ApprovalStore(settings.checkpoint_database_url)
        async with AsyncPostgresSaver.from_conn_string(
                psycopg_url(settings.checkpoint_database_url),
                serde=checkpoint_serializer()) as saver:
            await saver.setup()
            await store.setup()
            if test_model is not None:
                app.state.workflow = build_workflow(
                    test_model, test_tools, checkpointer=saver, approval_store=store)
                app.state.approval_store = store
                app.state.model = test_model
                yield
            else:
                engine = create_async_engine(settings.reader_database_url)
                try:
                    async with ClientGroup({
                        "data": Client(data_server(engine)),
                        "knowledge": Client(build_real_server(settings.milvus_uri)),
                    }) as tools:
                        model = select_chat_client(settings.provider)
                        app.state.workflow = build_workflow(
                            model, tools, checkpointer=saver, approval_store=store)
                        app.state.approval_store = store
                        app.state.model = model
                        yield
                finally:
                    await engine.dispose()

    app = FastAPI(title="M7 local durable review", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "model_response_count": len(
            getattr(app.state.model, "response_models", []))}

    async def inspect(run_id: str):
        snapshot = await app.state.workflow.aget_state(_config(run_id))
        if not snapshot.values or snapshot.values.get("run_id") != run_id:
            raise HTTPException(status_code=404, detail="run not found")
        waiting = ("human_review" in snapshot.next
                   and any(task.interrupts for task in snapshot.tasks))
        status = "WAITING_APPROVAL" if waiting else snapshot.values.get("status", "UNKNOWN")
        return snapshot, status

    @app.post("/runs", status_code=201)
    async def create_run(request: CreateRun):
        run_id = str(uuid4())
        model = app.state.model
        before = len(getattr(model, "response_models", []))
        await app.state.workflow.ainvoke(
            {"run_id": run_id, "user_query": request.query.strip(), "trace": []},
            _config(run_id))
        _, status = await inspect(run_id)
        observed = getattr(model, "response_models", [])[before:]
        return {"run_id": run_id, "thread_id": run_id, "status": status,
                "model_provenance": {
                    "provider": getattr(model, "provider", None),
                    "requested_model": getattr(model, "model", None),
                    "observed_response_model_ids": sorted({item for item in observed
                                                          if item is not None}),
                    "response_model_missing_count": observed.count(None),
                    "response_count": len(observed)}}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str):
        snapshot, status = await inspect(run_id)
        values = snapshot.values
        analysis = values.get("analysis")
        return {"run_id": run_id, "thread_id": run_id, "status": status,
                "next": list(snapshot.next), "trace": values.get("trace", []),
                "verification": values["verification"].model_dump()
                if "verification" in values else None,
                "analysis": analysis.model_dump() if analysis else None,
                "approval": values["approval"].model_dump()
                if "approval" in values else None,
                "publication": values["publication"].model_dump()
                if "publication" in values else None}

    @app.post("/runs/{run_id}/review")
    async def review_run(run_id: str, request: ReviewRun,
                         x_review_token: str | None = Header(default=None)):
        if (x_review_token is None
                or not secrets.compare_digest(x_review_token, settings.review_token)):
            raise HTTPException(status_code=403, detail="reviewer authorization required")
        _, status = await inspect(run_id)
        if status != "WAITING_APPROVAL":
            raise HTTPException(status_code=409, detail="run is not waiting for approval")
        approval = await app.state.approval_store.record_decision(
            run_id, status="APPROVED" if request.decision == "approve" else "REJECTED",
            reviewer_id=settings.reviewer_id, comment=request.comment)
        await app.state.workflow.ainvoke(
            Command(resume={"approval_id": approval.approval_id}), _config(run_id))
        _, final_status = await inspect(run_id)
        return {"run_id": run_id, "status": final_status,
                "approval_id": approval.approval_id}

    return app


def app_from_env() -> FastAPI:
    """Uvicorn factory; configuration and connections are created at process start."""
    return create_app()
