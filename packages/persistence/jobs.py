"""PostgreSQL lease queue for one durable LangGraph thread per run."""

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from packages.persistence.approvals import psycopg_url


class LeaseLost(RuntimeError):
    """The job was reclaimed; this worker must stop writing."""


class JobQueue:
    def __init__(self, database_url: str):
        self.database_url = psycopg_url(database_url)

    async def setup(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_jobs (
                    id uuid PRIMARY KEY,
                    run_id uuid NOT NULL,
                    kind text NOT NULL CHECK (kind IN ('START', 'RESUME')),
                    payload jsonb NOT NULL,
                    status text NOT NULL CHECK (status IN
                        ('PENDING', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED')),
                    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                    locked_by text,
                    lock_token uuid,
                    locked_until timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    last_error text,
                    CHECK ((status = 'RUNNING') = (locked_by IS NOT NULL AND
                        lock_token IS NOT NULL AND locked_until IS NOT NULL))
                )
            """)
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS agent_jobs_one_active_run
                ON agent_jobs (run_id)
                WHERE status IN ('PENDING', 'RUNNING')
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS agent_jobs_claim
                ON agent_jobs (status, locked_until, created_at)
            """)

    async def enqueue(self, run_id: str, kind: str, payload: dict) -> dict:
        UUID(run_id)
        if kind not in {"START", "RESUME"}:
            raise ValueError("invalid job kind")
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute("""
                INSERT INTO agent_jobs (id, run_id, kind, payload, status)
                VALUES (%s, %s, %s, %s, 'PENDING')
                RETURNING *
            """, (str(uuid4()), run_id, kind, Jsonb(payload)))).fetchone()
        return row

    async def claim(self, worker_id: str, lease_seconds: int = 30) -> dict | None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("worker ID and positive lease are required")
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                row = await (await conn.execute("""
                    SELECT id FROM agent_jobs
                    WHERE status = 'PENDING' OR
                        (status = 'RUNNING' AND locked_until < now())
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                """)).fetchone()
                if row is None:
                    return None
                claimed = await (await conn.execute("""
                    UPDATE agent_jobs SET status = 'RUNNING', attempt = attempt + 1,
                        locked_by = %s, lock_token = %s,
                        locked_until = now() + (%s * interval '1 second'),
                        updated_at = now()
                    WHERE id = %s RETURNING *
                """, (worker_id, str(uuid4()), lease_seconds, row["id"]))).fetchone()
        return claimed

    async def assert_lease(self, job: dict) -> None:
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute("""
                SELECT 1 FROM agent_jobs WHERE id = %s AND status = 'RUNNING'
                  AND lock_token = %s AND locked_until > now()
            """, (job["id"], job["lock_token"]))).fetchone()
        if row is None:
            raise LeaseLost("worker lease expired or was reclaimed")

    async def renew(self, job: dict, lease_seconds: int = 30) -> None:
        async with await AsyncConnection.connect(self.database_url) as conn:
            result = await conn.execute("""
                UPDATE agent_jobs SET locked_until = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE id = %s AND status = 'RUNNING' AND lock_token = %s
                  AND locked_until > now()
            """, (lease_seconds, job["id"], job["lock_token"]))
        if result.rowcount != 1:
            raise LeaseLost("cannot renew a lost worker lease")

    async def finish(self, job: dict, status: str, error: str | None = None) -> None:
        if status not in {"WAITING_APPROVAL", "COMPLETED", "FAILED"}:
            raise ValueError("invalid terminal job status")
        async with await AsyncConnection.connect(self.database_url) as conn:
            result = await conn.execute("""
                UPDATE agent_jobs SET status = %s, locked_by = NULL,
                    lock_token = NULL, locked_until = NULL, updated_at = now(),
                    last_error = %s
                WHERE id = %s AND status = 'RUNNING' AND lock_token = %s
                  AND locked_until > now()
            """, (status, error, job["id"], job["lock_token"]))
        if result.rowcount != 1:
            raise LeaseLost("cannot finish a lost worker lease")

    async def latest(self, run_id: str) -> dict | None:
        UUID(run_id)
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            return await (await conn.execute("""
                SELECT * FROM agent_jobs WHERE run_id = %s
                ORDER BY created_at DESC, id DESC LIMIT 1
            """, (run_id,))).fetchone()

    @asynccontextmanager
    async def execution_lock(self, job: dict, lease_seconds: int):
        """Serialize checkpoint writers for a run, including a lease takeover."""
        key = f"m9:checkpoint:{job['run_id']}"
        async with await AsyncConnection.connect(self.database_url,
                                                  autocommit=True) as conn:
            acquired = False
            try:
                while not acquired:
                    row = await (await conn.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s))", (key,))).fetchone()
                    acquired = row[0]
                    if not acquired:
                        await self.renew(job, lease_seconds)
                        await asyncio.sleep(min(0.25, lease_seconds / 4))
                await self.assert_lease(job)
                yield
            finally:
                if acquired:
                    await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
