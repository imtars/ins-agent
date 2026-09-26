import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from packages.domain.hf_download import DATASETS, verify_manifest
from packages.domain.synthetic import PRODUCT_PROFILES
from packages.persistence.synthetic_loader import database_snapshot


@pytest.fixture(scope="module")
def database_url():
    url = os.environ.get("M1_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set M1_TEST_DATABASE_URL to a migrated, populated disposable database")
    return url


def test_counts_foreign_keys_and_manifest_hash(database_url):
    async def check():
        engine = create_async_engine(database_url)
        try:
            counts, hashes = await database_snapshot(engine)
            async with engine.connect() as connection:
                assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "20260927_02"
                product_columns = {row[0] for row in (await connection.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='products'"
                ))).all()}
                assert product_columns == {"id", "product_code", "name", "product_type",
                                           "annual_base_premium"}
                orphan_queries = (
                    "SELECT count(*) FROM agents a LEFT JOIN branches b ON a.branch_id=b.id WHERE b.id IS NULL",
                    "SELECT count(*) FROM customers c LEFT JOIN branches b ON c.home_branch_id=b.id WHERE b.id IS NULL",
                    "SELECT count(*) FROM policies p LEFT JOIN customers c ON p.customer_id=c.id WHERE c.id IS NULL",
                    "SELECT count(*) FROM policies p LEFT JOIN agents a ON p.agent_id=a.id WHERE a.id IS NULL",
                    "SELECT count(*) FROM policies p LEFT JOIN products d ON p.product_id=d.id WHERE d.id IS NULL",
                    "SELECT count(*) FROM claims c LEFT JOIN policies p ON c.policy_id=p.id WHERE p.id IS NULL",
                    "SELECT count(*) FROM claim_payments cp LEFT JOIN claims c ON cp.claim_id=c.id WHERE c.id IS NULL",
                )
                for query in orphan_queries:
                    assert (await connection.execute(text(query))).scalar_one() == 0
                constraints = {row[0] for row in (await connection.execute(text(
                    "SELECT conname FROM pg_constraint WHERE connamespace='public'::regnamespace"
                ))).all()}
                assert {"customer_risk_score_range", "product_type_valid",
                        "product_premium_positive", "policy_dates_ordered",
                        "policy_premium_positive", "claim_amount_positive",
                        "claim_status_valid", "payment_amount_positive"} <= constraints
                assert (await connection.execute(text(
                    "SELECT count(*) FROM claims c JOIN policies p ON c.policy_id=p.id "
                    "WHERE c.claim_date<p.start_date OR c.claim_date>=p.end_date"
                ))).scalar_one() == 0
                for profile in PRODUCT_PROFILES:
                    if profile.waiting_days:
                        assert (await connection.execute(text(
                            "SELECT count(*) FROM claims c JOIN policies p ON c.policy_id=p.id "
                            "JOIN products d ON p.product_id=d.id "
                            "WHERE d.product_code=:code AND "
                            "c.claim_date<p.start_date+CAST(:waiting AS INTEGER)"
                        ), {"code": profile.code, "waiting": profile.waiting_days})).scalar_one() == 0
                assert (await connection.execute(text(
                    "SELECT count(*) FROM policies p JOIN agents a ON p.agent_id=a.id "
                    "WHERE p.branch_id<>a.branch_id"
                ))).scalar_one() == 0
                assert (await connection.execute(text(
                    "SELECT count(*) FROM claims c LEFT JOIN "
                    "(SELECT claim_id, sum(amount) AS paid FROM claim_payments GROUP BY claim_id) p "
                    "ON c.id=p.claim_id WHERE (c.status='settled' AND "
                    "(p.paid IS NULL OR p.paid<>c.claim_amount)) "
                    "OR (c.status<>'settled' AND p.paid IS NOT NULL)"
                ))).scalar_one() == 0
            return counts, hashes
        finally:
            await engine.dispose()

    counts, hashes = asyncio.run(check())
    manifest = json.loads(Path("data/synthetic/manifest.json").read_text(encoding="utf-8"))
    assert counts == manifest["table_counts"]
    assert hashes == manifest["table_sha256"]
    assert counts["branches"] == 12 and counts["products"] == 12
    assert counts["customers"] == 10_000 and counts["policies"] == 30_000


def test_check_and_foreign_key_constraints(database_url):
    async def check():
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(text(
                            "INSERT INTO customers(id,customer_code,home_branch_id,birth_date,risk_score) "
                            "VALUES (999999,'INVALID-RISK',1,'1990-01-01',101)"
                        ))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(text(
                            "INSERT INTO agents(id,agent_code,branch_id,joined_on) "
                            "VALUES (999999,'INVALID-FK',999999,'2020-01-01')"
                        ))
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(text(
                            "INSERT INTO products(id,product_code,name,product_type,annual_base_premium) "
                            "VALUES (999999,'INVALID-PREMIUM','invalid','motor',-1)"
                        ))
        finally:
            await engine.dispose()
    asyncio.run(check())


def test_document_product_codes_and_hashes(database_url):
    async def codes():
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return {row[0] for row in (await connection.execute(text(
                    "SELECT product_code FROM products"
                ))).all()}
        finally:
            await engine.dispose()
    codes = asyncio.run(codes())
    manifest = json.loads(Path("data/synthetic/manifest.json").read_text(encoding="utf-8"))
    documents = Path("data/synthetic/documents")
    assert set(manifest["document_sha256"]) == {f"{code}.md" for code in codes}
    for name, expected in manifest["document_sha256"].items():
        path = documents / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        assert f"product_code: {path.stem}\n" in path.read_text(encoding="utf-8")


def test_actual_download_provenance():
    if os.environ.get("M1_VERIFY_DOWNLOADS") != "1":
        pytest.skip("set M1_VERIFY_DOWNLOADS=1 after downloading public and HF files")
    for key in DATASETS:
        path = Path(f"data/manifests/{key}_download.json")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert len(manifest["revision"]) == 40
        assert manifest["retrieved_at"]
        assert {item["filename"] for item in manifest["files"]} == set(DATASETS[key]["files"])
        verify_manifest(path)
    public = json.loads(Path("data/manifests/public_docs_download.json").read_text(encoding="utf-8"))
    assert len(public["documents"]) == 5
    for item in public["documents"]:
        path = Path(item["local_path"])
        assert path.stat().st_size == item["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        assert item["status"] == "draft" and item["retrieved_at"]
        assert item["license"] == "not_stated_on_source_page" and item["usage_note"]
