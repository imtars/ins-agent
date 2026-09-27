"""Schema context and bounded execution using the dedicated PostgreSQL reader."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from packages.sql.security import BUSINESS_TABLES, validate_sql

MAX_ROWS = 200
STATEMENT_TIMEOUT_MS = 5_000
REGION_LABELS = {"north": "北部", "south": "南部", "east": "东部", "west": "西部"}
PRODUCT_TYPES = ("motor", "health", "accident", "life")
CLAIM_STATUSES = ("open", "settled", "denied")


def business_value_context() -> str:
    """Document stored categorical values that column types cannot convey."""
    regions = ", ".join(f"{label} -> '{code}'" for code, label in REGION_LABELS.items())
    return ("Stored business values (translate labels to codes for SQL filters):\n"
            f"branches.region: {regions}; the column stores only English codes. "
            "For example, 北部 means WHERE branches.region = 'north', never '北部'.\n"
            f"products.product_type: {', '.join(PRODUCT_TYPES)}\n"
            f"claims.status: {', '.join(CLAIM_STATUSES)}")


def require_reader(engine: AsyncEngine) -> None:
    if make_url(str(engine.url)).username != "insurance_reader":
        raise ValueError("M3 SQL execution requires insurance_reader credentials")


async def schema_context(engine: AsyncEngine) -> str:
    require_reader(engine)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT table_name,column_name,data_type FROM information_schema.columns "
            "WHERE table_schema='public' ORDER BY table_name,ordinal_position"
        ))).all()
        foreign_keys = (await conn.execute(text(
            "SELECT src.relname,sa.attname,dst.relname,da.attname "
            "FROM pg_catalog.pg_constraint c "
            "JOIN pg_catalog.pg_class src ON src.oid=c.conrelid "
            "JOIN pg_catalog.pg_namespace ns ON ns.oid=src.relnamespace "
            "JOIN pg_catalog.pg_class dst ON dst.oid=c.confrelid "
            "JOIN LATERAL unnest(c.conkey,c.confkey) AS pair(srcatt,dstatt) ON true "
            "JOIN pg_catalog.pg_attribute sa ON sa.attrelid=src.oid AND sa.attnum=pair.srcatt "
            "JOIN pg_catalog.pg_attribute da ON da.attrelid=dst.oid AND da.attnum=pair.dstatt "
            "WHERE ns.nspname='public' AND c.contype='f' "
            "ORDER BY src.relname,sa.attname"
        ))).all()
    grouped: dict[str, list[str]] = {name: [] for name in sorted(BUSINESS_TABLES)}
    for table, column, kind in rows:
        if table in grouped:
            grouped[table].append(f"{column} {kind}")
    if any(not columns for columns in grouped.values()):
        raise RuntimeError("business schema is incomplete")
    relations = [f"{table}.{column} -> {target}.{target_column}"
                 for table, column, target, target_column in foreign_keys
                 if table in BUSINESS_TABLES and target in BUSINESS_TABLES]
    return ("\n".join(f"{table}({', '.join(grouped[table])})" for table in sorted(grouped))
            + "\nForeign keys:\n" + "\n".join(relations)
            + "\n" + business_value_context())


def json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


async def execute_readonly(engine: AsyncEngine, sql: str, *, max_rows: int = MAX_ROWS,
                           timeout_ms: int = STATEMENT_TIMEOUT_MS) -> list[dict]:
    require_reader(engine)
    query = validate_sql(sql)
    if not 1 <= max_rows <= MAX_ROWS or not 100 <= timeout_ms <= STATEMENT_TIMEOUT_MS:
        raise ValueError("execution limits out of range")
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            await conn.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                               {"value": f"{timeout_ms}ms"})
            result = await conn.stream(text(query))
            rows = []
            async for row in result.mappings():
                if len(rows) >= max_rows:
                    raise ValueError(f"query exceeds {max_rows} returned rows")
                rows.append({key: json_value(value) for key, value in row.items()})
            return rows
        finally:
            await transaction.rollback()
