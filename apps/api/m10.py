"""M10 authenticated REST/SSE facade over the durable M8/M9 worker."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from apps.api.m8 import CreateRun, ReviewRun, config
from apps.api.run_control import RunControl
from packages.agent.m8_graph import (ANALYSIS_ADAPTER, RAG_ADAPTER, SQL_ADAPTER,
                                      VERIFICATION_ADAPTER, build_m8_workflow)
from packages.knowledge.catalog import document_metadata, source_for
from packages.knowledge.documents import registered_sources
from packages.persistence.approvals import psycopg_url
from packages.persistence.artifacts import ArtifactStore
from packages.persistence.auth import AuthStore
from packages.persistence.checkpoints import checkpoint_serializer
from packages.persistence.events import EventStore
from packages.persistence.jobs import JobQueue
from packages.persistence.reviews import ReviewStore


REPORTS = {"rag": Path("evaluation/reports/rag_ablation.json"),
           "sql": Path("evaluation/reports/sql_evaluation.json")}
ADAPTERS = {"sql": SQL_ADAPTER, "rag": RAG_ADAPTER,
            "analysis": ANALYSIS_ADAPTER, "verification": VERIFICATION_ADAPTER}
BEARER = HTTPBearer(auto_error=False)


class M10Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="M10_", env_file=".env", extra="ignore")
    database_url: str
    reader_database_url: str
    jwt_secret: str = Field(min_length=32)


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class RegisterDocument(BaseModel):
    doc_id: str = Field(min_length=1, max_length=150)


def create_app(settings: M10Settings | None = None) -> FastAPI:
    settings = settings or M10Settings()
    primary, reader = make_url(settings.database_url), make_url(settings.reader_database_url)
    if primary.database == reader.database and primary.host in {reader.host, "localhost", "127.0.0.1"}:
        raise ValueError("M10 state database must differ from the business database")
    auth = AuthStore(settings.database_url, settings.jwt_secret)
    queue = JobQueue(settings.database_url)
    artifacts = ArtifactStore(settings.database_url)
    reviews = ReviewStore(settings.database_url, artifacts)
    events = EventStore(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncPostgresSaver.from_conn_string(
                psycopg_url(settings.database_url),
                serde=checkpoint_serializer()) as saver:
            await saver.setup()
            for store in (auth, queue, artifacts, reviews, events):
                await store.setup()

            async def no_lease():
                return None

            graph = build_m8_workflow(None, None, artifacts, reviews,
                checkpointer=saver, assert_lease=no_lease)
            app.state.control = RunControl(queue, graph, reviews)
            app.state.events = events
            yield

    app = FastAPI(title="Insurance Agent Harness API", lifespan=lifespan)

    async def current(credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)):
        if credentials is None:
            raise HTTPException(401, "bearer token required")
        try:
            user = await auth.authenticate(credentials.credentials)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(401, "invalid access token") from exc
        if user is None:
            raise HTTPException(401, "inactive or unknown account")
        return user

    def role(*allowed: str):
        async def require(user=Depends(current)):
            if user["role"] not in allowed:
                raise HTTPException(403, "role does not permit this action")
            return user
        return require

    async def run_or_404(run_id: str) -> dict:
        return await app.state.control.get(run_id)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/auth/login")
    async def login(request: LoginRequest):
        result = await auth.login(request.username, request.password)
        if result is None:
            raise HTTPException(401, "invalid credentials")
        return result

    @app.post("/api/auth/refresh")
    async def refresh(request: RefreshRequest):
        try:
            result = await auth.refresh(request.refresh_token)
        except jwt.InvalidTokenError as exc:
            raise HTTPException(401, "invalid refresh token") from exc
        if result is None:
            raise HTTPException(401, "revoked or expired refresh token")
        return result

    @app.get("/api/auth/me")
    async def me(user=Depends(current)):
        return user

    @app.post("/api/runs", status_code=202)
    async def create_run(request: CreateRun, user=Depends(role("analyst", "reviewer", "admin"))):
        result = await app.state.control.create(request)
        await events.append(result["run_id"], "workflow.queued",
                            job_id=result["job_id"], payload={"submitted_by": user["username"]})
        return result

    @app.get("/api/runs")
    async def list_runs(status: str | None = None, limit: int = Query(50, ge=1, le=100),
                        user=Depends(current)):
        if status and status not in {"PENDING", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED"}:
            raise HTTPException(422, "invalid status")
        async with await AsyncConnection.connect(psycopg_url(settings.database_url), row_factory=dict_row) as conn:
            rows = await (await conn.execute("""
                SELECT DISTINCT ON (run_id) run_id, id, kind, status, attempt,
                    created_at, updated_at, last_error, payload
                FROM agent_jobs ORDER BY run_id, created_at DESC, id DESC
            """)).fetchall()
        filtered = [row for row in rows if status is None or row["status"] == status]
        filtered.sort(key=lambda row: row["updated_at"], reverse=True)
        return {"runs": [{"run_id": str(row["run_id"]), "job_id": str(row["id"]),
                          "status": row["status"], "attempt": row["attempt"],
                          "updated_at": row["updated_at"].isoformat(),
                          "query": row["payload"].get("query") if row["kind"] == "START" else None}
                         for row in filtered[:limit]]}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, user=Depends(current)):
        return await run_or_404(run_id)

    @app.post("/api/runs/{run_id}/review", status_code=202)
    async def review_run(run_id: str, request: ReviewRun,
                         user=Depends(role("reviewer", "admin"))):
        result = await app.state.control.review(run_id, request, user["username"])
        await events.append(run_id, "review.decided", job_id=result["job_id"],
                            payload={"decision": request.decision,
                                     "rerun_targets": request.rerun_targets,
                                     "reviewer": user["username"]})
        return result

    @app.get("/api/runs/{run_id}/events")
    async def get_events(run_id: str, after_id: int = Query(0, ge=0),
                         user=Depends(current)):
        await run_or_404(run_id)
        return {"events": await events.list(run_id, after_id=after_id)}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request,
                     after_id: int = Query(0, ge=0), user=Depends(current),
                     credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)):
        await run_or_404(run_id)

        async def generate():
            cursor = after_id
            while not await request.is_disconnected():
                try:
                    if not credentials or not await auth.authenticate(credentials.credentials):
                        break
                except jwt.InvalidTokenError:
                    break
                batch = await events.list(run_id, after_id=cursor)
                for item in batch:
                    cursor = item["id"]
                    yield f"id: {cursor}\nevent: {item['event_type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if not batch:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}/artifacts")
    async def get_artifacts(run_id: str, user=Depends(current)):
        record = await run_or_404(run_id)
        result = {}
        for stage, raw in record["refs"].items():
            from packages.persistence.artifacts import ArtifactRef
            ref = ArtifactRef.model_validate(raw)
            value = await artifacts.load(ref, ADAPTERS[stage])
            result[stage] = {"ref": raw, "content": ADAPTERS[stage].dump_python(value, mode="json")}
        return {"run_id": run_id, "artifacts": result}

    @app.get("/api/runs/{run_id}/report")
    async def get_report(run_id: str, user=Depends(current)):
        record = await run_or_404(run_id)
        if "analysis" not in record["refs"]:
            raise HTTPException(404, "analysis is not available")
        from packages.persistence.artifacts import ArtifactRef
        ref = ArtifactRef.model_validate(record["refs"]["analysis"])
        analysis = await artifacts.load(ref, ANALYSIS_ADAPTER)
        return {"run_id": run_id, "status": record["status"],
                "published": bool(record["publication"]),
                "publication": record["publication"],
                "artifact": ref.model_dump(), "analysis": analysis.model_dump()}

    @app.get("/api/documents")
    async def list_documents(user=Depends(current)):
        return {"documents": [document_metadata(item.doc_id) for item in registered_sources()]}

    @app.post("/api/documents")
    async def register_document(request: RegisterDocument, user=Depends(role("admin"))):
        try:
            source = source_for(request.doc_id)
            metadata = document_metadata(request.doc_id)
            if hashlib.sha256(source.path.read_bytes()).hexdigest() != source.sha256:
                raise ValueError("source SHA-256 mismatch")
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        # Documents enter this API only through the audited M2 source registry.
        # This endpoint verifies and acknowledges an existing registered source;
        # it never mutates the indexed corpus.
        return {**metadata, "registration": "verified_existing_source"}

    @app.post("/api/evaluations/{kind}")
    async def register_evaluation(kind: str, user=Depends(role("admin"))):
        if kind not in REPORTS:
            raise HTTPException(404, "unknown evaluation")
        path = REPORTS[kind]
        if not path.is_file():
            raise HTTPException(404, "accepted report is not available")
        raw = path.read_bytes()
        return {"id": kind, "status": "existing_report", "sha256": hashlib.sha256(raw).hexdigest(),
                "report": json.loads(raw)}

    @app.get("/api/evaluations/{kind}")
    async def get_evaluation(kind: str, user=Depends(current)):
        if kind not in REPORTS:
            raise HTTPException(404, "unknown evaluation")
        path = REPORTS[kind]
        if not path.is_file():
            raise HTTPException(404, "accepted report is not available")
        raw = path.read_bytes()
        report = json.loads(raw)
        return {"id": kind, "status": "existing_report",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "metrics": report.get("results") if kind == "rag" else report.get("metrics"),
                "report": report}

    return app


def app_from_env():
    return create_app()
