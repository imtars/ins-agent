"""M7 approval decisions and idempotent publication receipts in PostgreSQL."""

import hashlib
import json
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from packages.agent.models import (AnalysisResult, ApprovalResult, PublicationReceipt,
                                   VerificationResult)


class PublishGuardViolation(ValueError):
    """Publication lacks a matching, authorized approval and PASS verdict."""


def psycopg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url.split("://", 1)[1]
    if url.startswith(("postgresql://", "postgres://")):
        return url
    raise ValueError("M7 checkpoint URL must use PostgreSQL")


def canonical_hash(analysis: AnalysisResult) -> str:
    payload = json.dumps(analysis.model_dump(), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ApprovalStore:
    def __init__(self, database_url: str):
        self.database_url = psycopg_url(database_url)

    async def setup(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_approvals (
                    run_id uuid PRIMARY KEY,
                    approval_id uuid NOT NULL UNIQUE,
                    status text NOT NULL CHECK (status IN ('APPROVED', 'REJECTED')),
                    reviewer_id text NOT NULL CHECK (length(reviewer_id) > 0),
                    comment text NOT NULL DEFAULT '',
                    decided_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_publications (
                    run_id uuid PRIMARY KEY,
                    approval_id uuid NOT NULL REFERENCES agent_approvals(approval_id),
                    content_hash char(64) NOT NULL,
                    published_at timestamptz NOT NULL DEFAULT now()
                )
            """)

    async def get_decision(self, run_id: str) -> ApprovalResult | None:
        UUID(run_id)
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute(
                "SELECT run_id, approval_id, status, reviewer_id, comment "
                "FROM agent_approvals WHERE run_id = %s", (run_id,))).fetchone()
        return self._approval(row) if row else None

    @staticmethod
    def _approval(row: dict) -> ApprovalResult:
        return ApprovalResult(run_id=str(row["run_id"]),
                              approval_id=str(row["approval_id"]),
                              status=row["status"], reviewer_id=row["reviewer_id"],
                              comment=row["comment"])

    async def record_decision(self, run_id: str, *, status: str, reviewer_id: str,
                              comment: str = "") -> ApprovalResult:
        UUID(run_id)
        if status not in {"APPROVED", "REJECTED"} or not reviewer_id.strip():
            raise ValueError("invalid approval decision or reviewer")
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO agent_approvals
                        (run_id, approval_id, status, reviewer_id, comment)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                """, (run_id, str(uuid4()), status, reviewer_id, comment))
                row = await (await conn.execute(
                    "SELECT run_id, approval_id, status, reviewer_id, comment "
                    "FROM agent_approvals WHERE run_id = %s", (run_id,))).fetchone()
        result = self._approval(row)
        if (result.status, result.reviewer_id, result.comment) != (status, reviewer_id, comment):
            raise ValueError("run already has a different review decision")
        return result

    async def get_publication(self, run_id: str) -> PublicationReceipt | None:
        UUID(run_id)
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute(
                "SELECT run_id, approval_id, content_hash FROM agent_publications "
                "WHERE run_id = %s", (run_id,))).fetchone()
        return self._receipt(row) if row else None

    @staticmethod
    def _receipt(row: dict) -> PublicationReceipt:
        return PublicationReceipt(run_id=str(row["run_id"]),
                                  approval_id=str(row["approval_id"]),
                                  content_hash=row["content_hash"].strip())

    async def publish(self, *, run_id: str, approval: ApprovalResult,
                      verification: VerificationResult,
                      analysis: AnalysisResult) -> PublicationReceipt:
        UUID(run_id)
        if (verification.status != "PASS" or verification.issues
                or approval.status != "APPROVED" or approval.run_id != run_id):
            raise PublishGuardViolation("publish requires PASS and approved matching run")
        content_hash = canonical_hash(analysis)
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                row = await (await conn.execute(
                    "SELECT approval_id, status, reviewer_id, comment "
                    "FROM agent_approvals WHERE run_id = %s FOR UPDATE", (run_id,)
                )).fetchone()
                if (row is None or str(row["approval_id"]) != approval.approval_id
                        or row["status"] != "APPROVED"
                        or row["reviewer_id"] != approval.reviewer_id
                        or row["comment"] != approval.comment):
                    raise PublishGuardViolation("approval does not match stored reviewer decision")
                await conn.execute("""
                    INSERT INTO agent_publications (run_id, approval_id, content_hash)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                """, (run_id, approval.approval_id, content_hash))
                receipt = await (await conn.execute(
                    "SELECT run_id, approval_id, content_hash "
                    "FROM agent_publications WHERE run_id = %s", (run_id,))).fetchone()
        result = self._receipt(receipt)
        if result.approval_id != approval.approval_id or result.content_hash != content_hash:
            raise PublishGuardViolation("existing publication conflicts with approved content")
        return result
