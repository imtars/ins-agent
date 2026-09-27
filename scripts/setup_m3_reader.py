"""Create or refresh the local M3 SELECT-only PostgreSQL login."""

import argparse
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.domain.config import get_settings

TABLES = "branches, agents, customers, products, policies, claims, claim_payments"


async def setup(database_url: str, password: str) -> None:
    if len(password) < 16:
        raise ValueError("M3_READER_PASSWORD must contain at least 16 characters")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            exists = (await conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='insurance_reader')"
            ))).scalar_one()
            if not exists:
                await conn.execute(text("CREATE ROLE insurance_reader NOINHERIT"))
            statement = (await conn.execute(text(
                "SELECT format('ALTER ROLE insurance_reader LOGIN PASSWORD %L', CAST(:password AS text))"
            ), {"password": password})).scalar_one()
            await conn.exec_driver_sql(statement)
            await conn.execute(text("ALTER ROLE insurance_reader SET default_transaction_read_only = on"))
            await conn.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM insurance_reader"))
            await conn.execute(text("REVOKE ALL ON SCHEMA public FROM insurance_reader"))
            await conn.execute(text("GRANT USAGE ON SCHEMA public TO insurance_reader"))
            await conn.execute(text(f"GRANT SELECT ON {TABLES} TO insurance_reader"))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=get_settings().database_url)
    args = parser.parse_args()
    password = os.environ.get("M3_READER_PASSWORD")
    if not password:
        raise ValueError("set M3_READER_PASSWORD in the environment")
    asyncio.run(setup(args.database_url, password))
    print("insurance_reader SELECT grants configured for target database")


if __name__ == "__main__":
    main()
