"""Canonical hashing and PostgreSQL loading for synthetic M1 data."""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import MetaData, Table, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.domain.synthetic import GENERATOR_VERSION, TABLE_ORDER, SyntheticConfig, SyntheticDataset

SCHEMA_REVISION = "20260927_01"


def normalize(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalize(item) for item in value]
    return value


def canonical_bytes(value) -> bytes:
    return json.dumps(normalize(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def hash_rows(rows) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_bytes(dict(row)))
        digest.update(b"\n")
    return digest.hexdigest()


def generated_table_hashes(dataset: SyntheticDataset) -> dict[str, str]:
    return {name: hash_rows(dataset.tables[name]) for name in TABLE_ORDER}


def document_hashes(documents: dict[str, str]) -> dict[str, str]:
    return {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for name, text in sorted(documents.items())}


def dataset_hash(config: SyntheticConfig, tables: dict[str, str], docs: dict[str, str]) -> str:
    envelope = {"generator_version": GENERATOR_VERSION, "schema_revision": SCHEMA_REVISION,
                "config": config.to_dict(), "table_hashes": tables, "document_hashes": docs}
    return hashlib.sha256(canonical_bytes(envelope)).hexdigest()


async def reflected_tables(connection) -> dict[str, Table]:
    metadata = MetaData()
    result = {}
    for name in TABLE_ORDER:
        result[name] = await connection.run_sync(
            lambda sync_connection, table_name=name: Table(
                table_name, metadata, autoload_with=sync_connection
            )
        )
    return result


async def database_snapshot(engine: AsyncEngine) -> tuple[dict[str, int], dict[str, str]]:
    counts: dict[str, int] = {}
    hashes: dict[str, str] = {}
    async with engine.connect() as connection:
        tables = await reflected_tables(connection)
        for name in TABLE_ORDER:
            table = tables[name]
            counts[name] = (await connection.execute(select(func.count()).select_from(table))).scalar_one()
            result = await connection.stream(select(table).order_by(table.c.id))
            digest = hashlib.sha256()
            async for row in result.mappings():
                digest.update(canonical_bytes(dict(row)))
                digest.update(b"\n")
            hashes[name] = digest.hexdigest()
    return counts, hashes


async def load_dataset(engine: AsyncEngine, dataset: SyntheticDataset, batch_size: int = 1000) -> None:
    async with engine.begin() as connection:
        tables = await reflected_tables(connection)
        existing = {name: (await connection.execute(select(func.count()).select_from(table))).scalar_one()
                    for name, table in tables.items()}
        if any(existing.values()):
            raise ValueError(f"synthetic loader requires empty tables: {existing}")
        for name in TABLE_ORDER:
            rows = dataset.tables[name]
            for offset in range(0, len(rows), batch_size):
                await connection.execute(insert(tables[name]), rows[offset:offset + batch_size])


def write_documents(directory: Path, documents: dict[str, str]) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    stale = set(directory.glob("product_*.md")) - {directory / name for name in documents}
    if stale:
        raise ValueError(f"stale synthetic product documents: {sorted(stale)}")
    for name, content in documents.items():
        (directory / name).write_text(content, encoding="utf-8")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.glob("product_*.md"))}


def make_manifest(config: SyntheticConfig, counts: dict[str, int],
                  table_hashes: dict[str, str], doc_hashes: dict[str, str]) -> dict:
    return {
        "dataset_kind": "synthetic_insurance_operations",
        "generator_version": GENERATOR_VERSION,
        "schema_revision": SCHEMA_REVISION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": config.to_dict(),
        "normalization": "rows ordered by integer id; UTF-8 canonical JSON; sorted keys; Decimal without trailing zeros; ISO dates; one newline per row",
        "table_counts": counts,
        "table_sha256": table_hashes,
        "document_sha256": doc_hashes,
        "dataset_sha256": dataset_hash(config, table_hashes, doc_hashes),
        "synthetic_notice": "All operational records and product documents are fictional. No real personal data.",
    }


async def generate_and_load(database_url: str, config: SyntheticConfig,
                            document_dir: Path, manifest_path: Path) -> dict:
    from packages.domain.synthetic import generate

    dataset = generate(config)
    expected_hashes = generated_table_hashes(dataset)
    engine = create_async_engine(database_url)
    try:
        await load_dataset(engine, dataset)
        counts, actual_hashes = await database_snapshot(engine)
    finally:
        await engine.dispose()
    if actual_hashes != expected_hashes:
        raise RuntimeError("PostgreSQL rows differ from generated rows")
    if counts != {name: len(dataset.tables[name]) for name in TABLE_ORDER}:
        raise RuntimeError("PostgreSQL row counts differ from generated rows")
    doc_hashes = write_documents(document_dir, dataset.documents)
    if doc_hashes != document_hashes(dataset.documents):
        raise RuntimeError("written documents differ from generated documents")
    manifest = make_manifest(config, counts, actual_hashes, doc_hashes)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    return manifest
