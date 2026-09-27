"""Versioned M8 stage artifacts; checkpoint state carries only these references."""

import hashlib
import json
from typing import Awaitable, Callable, Literal, TypeVar
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from packages.persistence.approvals import psycopg_url


Stage = Literal["sql", "rag", "analysis", "verification"]
T = TypeVar("T")


class ArtifactConflict(ValueError):
    """A stored artifact does not match its immutable reference or content."""


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_id: str
    run_id: str
    stage: Stage
    generation: int = Field(ge=1)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ArtifactStore:
    def __init__(self, database_url: str):
        self.database_url = psycopg_url(database_url)

    async def setup(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS run_artifacts (
                    artifact_id uuid PRIMARY KEY,
                    run_id uuid NOT NULL,
                    stage text NOT NULL CHECK
                        (stage IN ('sql', 'rag', 'analysis', 'verification')),
                    generation integer NOT NULL CHECK (generation >= 1),
                    version integer NOT NULL CHECK (version >= 1),
                    content_json jsonb NOT NULL,
                    content_hash char(64) NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (run_id, stage, generation),
                    UNIQUE (run_id, stage, version)
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS run_artifacts_lookup
                ON run_artifacts (run_id, stage, version DESC)
            """)

    @staticmethod
    def _ref(row: dict) -> ArtifactRef:
        return ArtifactRef(artifact_id=str(row["artifact_id"]),
                           run_id=str(row["run_id"]), stage=row["stage"],
                           generation=row["generation"], version=row["version"],
                           content_hash=row["content_hash"].strip())

    async def get_generation(self, run_id: str, stage: Stage,
                             generation: int) -> ArtifactRef | None:
        UUID(run_id)
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute("""
                SELECT artifact_id, run_id, stage, generation, version, content_hash
                FROM run_artifacts
                WHERE run_id = %s AND stage = %s AND generation = %s
            """, (run_id, stage, generation))).fetchone()
        return self._ref(row) if row else None

    async def latest(self, run_id: str, stage: Stage) -> ArtifactRef | None:
        UUID(run_id)
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute("""
                SELECT artifact_id, run_id, stage, generation, version, content_hash
                FROM run_artifacts WHERE run_id = %s AND stage = %s
                ORDER BY version DESC LIMIT 1
            """, (run_id, stage))).fetchone()
        return self._ref(row) if row else None

    async def load(self, ref: ArtifactRef, adapter: TypeAdapter[T]) -> T:
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            row = await (await conn.execute("""
                SELECT run_id, stage, generation, version, content_json, content_hash
                FROM run_artifacts WHERE artifact_id = %s
            """, (ref.artifact_id,))).fetchone()
        if (row is None or str(row["run_id"]) != ref.run_id or row["stage"] != ref.stage
                or row["generation"] != ref.generation or row["version"] != ref.version
                or row["content_hash"].strip() != ref.content_hash
                or content_hash(row["content_json"]) != ref.content_hash):
            raise ArtifactConflict("artifact reference or stored hash mismatch")
        return adapter.validate_python(row["content_json"])

    async def load_or_compute(self, run_id: str, stage: Stage, generation: int,
                              adapter: TypeAdapter[T], compute: Callable[[], Awaitable[T]],
                              *, assert_lease: Callable[[], Awaitable[None]]) -> tuple[ArtifactRef, T]:
        await assert_lease()
        existing = await self.get_generation(run_id, stage, generation)
        if existing:
            return existing, await self.load(existing, adapter)
        value = adapter.validate_python(await compute())
        payload = adapter.dump_python(value, mode="json")
        digest = content_hash(payload)
        await assert_lease()
        async with await AsyncConnection.connect(self.database_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                # Serialize version allocation for this run/stage across workers.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                   (f"{run_id}:{stage}",))
                row = await (await conn.execute("""
                    SELECT artifact_id, run_id, stage, generation, version,
                           content_json, content_hash FROM run_artifacts
                    WHERE run_id = %s AND stage = %s AND generation = %s
                """, (run_id, stage, generation))).fetchone()
                if row is None:
                    version = await (await conn.execute("""
                        SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM run_artifacts
                        WHERE run_id = %s AND stage = %s
                    """, (run_id, stage))).fetchone()
                    row = await (await conn.execute("""
                        INSERT INTO run_artifacts
                            (artifact_id, run_id, stage, generation, version,
                             content_json, content_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING artifact_id, run_id, stage, generation, version,
                                  content_json, content_hash
                    """, (str(uuid4()), run_id, stage, generation, version["next_version"],
                          Jsonb(payload), digest))).fetchone()
        ref = self._ref(row)
        await assert_lease()
        return ref, await self.load(ref, adapter)
