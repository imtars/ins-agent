"""Small append-only run timeline, independent of checkpoint state."""

from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from packages.persistence.approvals import psycopg_url


class EventStore:
    def __init__(self, database_url: str):
        self.database_url = psycopg_url(database_url)

    async def setup(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS run_events (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    run_id uuid NOT NULL,
                    job_id uuid,
                    attempt integer NOT NULL DEFAULT 0,
                    event_type text NOT NULL,
                    node text,
                    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS run_events_run_id_id
                ON run_events (run_id, id)
            """)

    async def append(self, run_id: str, event_type: str, *, job_id: str | None = None,
                     attempt: int = 0, node: str | None = None,
                     payload: dict | None = None) -> dict:
        UUID(run_id)
        if not event_type or attempt < 0:
            raise ValueError("invalid event")
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute("""
                INSERT INTO run_events (run_id, job_id, attempt, event_type, node, payload)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
            """, (run_id, job_id, attempt, event_type, node,
                  Jsonb(payload or {})))).fetchone()
        return self._json(row)

    async def list(self, run_id: str, *, after_id: int = 0, limit: int = 200) -> list[dict]:
        UUID(run_id)
        if after_id < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid event cursor")
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            rows = await (await conn.execute("""
                SELECT * FROM run_events WHERE run_id = %s AND id > %s
                ORDER BY id LIMIT %s
            """, (run_id, after_id, limit))).fetchall()
        return [self._json(row) for row in rows]

    @staticmethod
    def _json(row: dict) -> dict:
        return {"id": row["id"], "run_id": str(row["run_id"]),
                "job_id": str(row["job_id"]) if row["job_id"] else None,
                "attempt": row["attempt"], "event_type": row["event_type"],
                "node": row["node"], "payload": row["payload"],
                "created_at": row["created_at"].isoformat()}
