"""Single API facade for M8 queue, checkpoint and review guard."""

from uuid import uuid4

from fastapi import HTTPException
from psycopg.errors import UniqueViolation

from apps.api.m8 import CreateRun, ReviewRun, config
from packages.persistence.approvals import PublishGuardViolation


class RunControl:
    def __init__(self, queue, graph, reviews):
        self.queue, self.graph, self.reviews = queue, graph, reviews

    async def create(self, request: CreateRun) -> dict:
        run_id = str(uuid4())
        job = await self.queue.enqueue(run_id, "START", {"query": request.query.strip()})
        return {"run_id": run_id, "thread_id": run_id,
                "job_id": str(job["id"]), "status": "PENDING"}

    async def get(self, run_id: str) -> dict:
        snapshot = await self.graph.aget_state(config(run_id))
        job = await self.queue.latest(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="run not found")
        values = snapshot.values or {}
        waiting = (snapshot.next == ("human_review",)
                   and any(task.interrupts for task in snapshot.tasks))
        refs = values.get("refs", {})
        return {"run_id": run_id, "thread_id": run_id,
                "status": "WAITING_APPROVAL" if waiting and job["status"] == "WAITING_APPROVAL"
                    else values.get("status", job["status"]) if job["status"] == "COMPLETED"
                    else job["status"],
                "job": {"id": str(job["id"]), "attempt": job["attempt"],
                        "status": job["status"], "locked_by": job["locked_by"],
                        "last_error": job["last_error"]},
                "cycle": values.get("cycle"), "next": list(snapshot.next),
                "trace": values.get("trace", []),
                "degraded_flags": values.get("degraded_flags", []),
                "refs": {key: ref.model_dump() for key, ref in refs.items()},
                "approval": values["approval"].model_dump() if values.get("approval") else None,
                "publication": values.get("publication")}

    async def review(self, run_id: str, request: ReviewRun, reviewer_id: str) -> dict:
        snapshot = await self.graph.aget_state(config(run_id))
        job = await self.queue.latest(run_id)
        if (job is None or job["status"] != "WAITING_APPROVAL"
                or snapshot.next != ("human_review",)
                or not any(task.interrupts for task in snapshot.tasks)):
            raise HTTPException(status_code=409, detail="run is not waiting for review")
        values = snapshot.values
        artifact = values["refs"]["analysis"]
        if (request.artifact_id != artifact.artifact_id
                or request.artifact_version != artifact.version
                or request.content_hash != artifact.content_hash):
            raise HTTPException(status_code=409, detail="reviewed artifact changed")
        targets = request.rerun_targets
        allowed = {"sql"} if values["plan"].route == "SQL" else (
            {"rag"} if values["plan"].route == "RAG" else {"sql", "rag"})
        if ((request.decision == "approve" and targets)
                or not set(targets) <= allowed | {"synthesis"}
                or len(set(targets)) != len(targets)):
            raise HTTPException(status_code=422, detail="invalid rerun targets for route")
        try:
            decision = await self.reviews.decide(run_id, values["cycle"], artifact,
                status="APPROVED" if request.decision == "approve" else "REJECTED",
                reviewer_id=reviewer_id, comment=request.comment,
                revision_targets=targets)
            queued = await self.queue.enqueue(run_id, "RESUME",
                {"approval_id": decision.approval_id})
        except (PublishGuardViolation, UniqueViolation) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": run_id, "approval_id": decision.approval_id,
                "job_id": str(queued["id"]), "status": "PENDING"}
