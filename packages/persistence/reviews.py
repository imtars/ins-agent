"""M8 review cycles bind a decision to an immutable analysis artifact."""

from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from packages.agent.models import VerificationResult
from packages.persistence.approvals import PublishGuardViolation, psycopg_url
from packages.persistence.artifacts import ArtifactRef, ArtifactStore


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    cycle: int = Field(ge=1)
    approval_id: str
    status: str
    reviewer_id: str
    comment: str
    revision_targets: list[str]
    artifact: ArtifactRef


class ReviewStore:
    def __init__(self, database_url: str, artifacts: ArtifactStore):
        self.database_url = psycopg_url(database_url)
        self.artifacts = artifacts

    async def setup(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS m8_reviews (
                    run_id uuid NOT NULL,
                    cycle integer NOT NULL CHECK (cycle >= 1),
                    approval_id uuid NOT NULL UNIQUE,
                    status text NOT NULL CHECK (status IN ('APPROVED', 'REJECTED')),
                    reviewer_id text NOT NULL CHECK (length(reviewer_id) > 0),
                    comment text NOT NULL DEFAULT '',
                    revision_targets jsonb NOT NULL,
                    artifact_id uuid NOT NULL REFERENCES run_artifacts(artifact_id),
                    artifact_version integer NOT NULL CHECK (artifact_version >= 1),
                    artifact_hash char(64) NOT NULL,
                    decided_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (run_id, cycle)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS m8_publications (
                    run_id uuid PRIMARY KEY,
                    approval_id uuid NOT NULL REFERENCES m8_reviews(approval_id),
                    artifact_id uuid NOT NULL REFERENCES run_artifacts(artifact_id),
                    artifact_version integer NOT NULL,
                    content_hash char(64) NOT NULL,
                    published_at timestamptz NOT NULL DEFAULT now()
                )
            """)

    @staticmethod
    def _decision(row: dict) -> ReviewDecision:
        return ReviewDecision(run_id=str(row["run_id"]), cycle=row["cycle"],
            approval_id=str(row["approval_id"]), status=row["status"],
            reviewer_id=row["reviewer_id"], comment=row["comment"],
            revision_targets=row["revision_targets"],
            artifact=ArtifactRef(artifact_id=str(row["artifact_id"]),
                run_id=str(row["run_id"]), stage="analysis", generation=row["cycle"],
                version=row["artifact_version"],
                content_hash=row["artifact_hash"].strip()))

    async def get(self, run_id: str, cycle: int) -> ReviewDecision | None:
        UUID(run_id)
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute("""
                SELECT * FROM m8_reviews WHERE run_id = %s AND cycle = %s
            """, (run_id, cycle))).fetchone()
        return self._decision(row) if row else None

    async def decide(self, run_id: str, cycle: int, artifact: ArtifactRef, *,
                     status: str, reviewer_id: str, comment: str = "",
                     revision_targets: list[str] | None = None) -> ReviewDecision:
        targets = revision_targets or []
        if (status not in {"APPROVED", "REJECTED"} or not reviewer_id.strip()
                or any(target not in {"sql", "rag", "synthesis"} for target in targets)
                or len(set(targets)) != len(targets)
                or (status == "APPROVED" and targets)
                or artifact.run_id != run_id or artifact.stage != "analysis"
                or artifact.generation != cycle):
            raise ValueError("invalid or mismatched review decision")
        latest = await self.artifacts.latest(run_id, "analysis")
        if latest != artifact:
            raise PublishGuardViolation("review target is no longer the current analysis")
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO m8_reviews (run_id, cycle, approval_id, status,
                        reviewer_id, comment, revision_targets, artifact_id,
                        artifact_version, artifact_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, cycle) DO NOTHING
                """, (run_id, cycle, str(uuid4()), status, reviewer_id, comment,
                      Jsonb(targets), artifact.artifact_id, artifact.version,
                      artifact.content_hash))
                row = await (await conn.execute("""
                    SELECT * FROM m8_reviews WHERE run_id = %s AND cycle = %s
                """, (run_id, cycle))).fetchone()
        decision = self._decision(row)
        if (decision.status != status or decision.reviewer_id != reviewer_id
                or decision.comment != comment or decision.revision_targets != targets
                or decision.artifact != artifact):
            raise PublishGuardViolation("review cycle already has a different decision")
        return decision

    async def publish(self, run_id: str, decision: ReviewDecision,
                      analysis_ref: ArtifactRef,
                      verification: VerificationResult) -> dict:
        if (decision.status != "APPROVED" or decision.run_id != run_id
                or decision.artifact != analysis_ref or verification.status != "PASS"
                or verification.issues):
            raise PublishGuardViolation("publication requires PASS on approved artifact")
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                row = await (await conn.execute("""
                    SELECT * FROM m8_reviews WHERE run_id = %s AND cycle = %s FOR UPDATE
                """, (run_id, decision.cycle))).fetchone()
                if row is None or self._decision(row) != decision:
                    raise PublishGuardViolation("decision does not match stored review")
                current = await (await conn.execute("""
                    SELECT artifact_id, version, content_hash FROM run_artifacts
                    WHERE run_id = %s AND stage = 'analysis'
                    ORDER BY version DESC LIMIT 1
                """, (run_id,))).fetchone()
                if (current is None or str(current["artifact_id"]) != analysis_ref.artifact_id
                        or current["version"] != analysis_ref.version
                        or current["content_hash"].strip() != analysis_ref.content_hash):
                    raise PublishGuardViolation("analysis changed after approval")
                await conn.execute("""
                    INSERT INTO m8_publications (run_id, approval_id, artifact_id,
                        artifact_version, content_hash)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                """, (run_id, decision.approval_id, analysis_ref.artifact_id,
                      analysis_ref.version, analysis_ref.content_hash))
                receipt = await (await conn.execute("""
                    SELECT run_id, approval_id, artifact_id, artifact_version,
                        content_hash FROM m8_publications WHERE run_id = %s
                """, (run_id,))).fetchone()
        result = {"run_id": str(receipt["run_id"]),
                  "approval_id": str(receipt["approval_id"]),
                  "artifact_id": str(receipt["artifact_id"]),
                  "artifact_version": receipt["artifact_version"],
                  "content_hash": receipt["content_hash"].strip()}
        if (result["approval_id"] != decision.approval_id
                or result["artifact_id"] != analysis_ref.artifact_id
                or result["content_hash"] != analysis_ref.content_hash):
            raise PublishGuardViolation("existing publication conflicts with review")
        return result
