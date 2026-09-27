import asyncio
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import yaml

from packages.sql.analytics import measures, period_summary
from packages.sql.agent import answer_question
from packages.sql.runtime import execute_readonly, schema_context
from packages.sql.security import UnsafeSQL, validate_sql
from evaluation.sql.run import rows_equal


@pytest.mark.parametrize("query", [
    "SELECT * FROM policies; DELETE FROM policies",
    "DELETE FROM policies",
    "UPDATE policies SET annual_premium=0",
    "SELECT * FROM policies FOR UPDATE",
    "SELECT * FROM pg_catalog.pg_class",
    "SELECT * FROM information_schema.tables",
    "SELECT pg_sleep(1) FROM policies",
    "SELECT set_config('statement_timeout','0',false) FROM policies",
    "SELECT * FROM generate_series(1,10)",
])
def test_reject_unsafe_sql(query):
    with pytest.raises(UnsafeSQL):
        validate_sql(query)


def test_accept_readonly_cte_and_reject_tableless():
    assert validate_sql("WITH x AS (SELECT id FROM policies) SELECT count(*) FROM x")
    with pytest.raises(UnsafeSQL):
        validate_sql("SELECT 1")


def test_analytics_denominators_are_exposure_weighted():
    row = {"product_code": "product_001", "region": "south", "exposure_days": 365,
           "earned_premium": "100.00", "claim_count": 2,
           "incurred_amount": "30.00", "paid_amount": "20.00"}
    value = measures(row)
    assert value["in_force_policy_years"] == "1.0000"
    assert value["incurred_loss_ratio"] == "0.3000"
    assert value["paid_loss_ratio"] == "0.2000"
    assert value["claims_per_in_force_policy_year"] == "2.0000"


def test_result_evaluator_uses_values_tolerance_and_requested_order():
    expected = [{"branch_code": "BR001", "ratio": "0.3000"},
                {"branch_code": "BR002", "ratio": "0.4000"}]
    reversed_rows = [{"label": "BR002", "value": "0.40001"},
                     {"label": "BR001", "value": "0.30001"}]
    assert rows_equal(expected, reversed_rows, "0.0001", ordered=False)
    assert not rows_equal(expected, reversed_rows, "0.0001", ordered=True)
    assert not rows_equal(expected, [{"branch_code": "BR001", "ratio": "0.9"},
                                     reversed_rows[0]], "0.0001", ordered=False)


class StubGenerator:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def generate(self, question, schema, feedback, attempt):
        self.calls.append((attempt, feedback))
        return self.responses[attempt]


def test_repair_attempts_are_capped_and_recorded(monkeypatch):
    async def context(_):
        return "policies(id integer)"

    async def execute(_, sql):
        return [{"n": 1}]

    monkeypatch.setattr("packages.sql.agent.schema_context", context)
    monkeypatch.setattr("packages.sql.agent.execute_readonly", execute)
    generator = StubGenerator([
        {"decision": "query", "sql": "DELETE FROM policies"},
        {"decision": "query", "sql": "SELECT count(*) AS n FROM policies"},
    ])
    result = asyncio.run(answer_question(None, generator, "保单数"))
    assert result.status == "success" and result.rows == [{"n": 1}]
    assert [item.stage for item in result.attempts] == ["validation_failed", "executed"]
    assert len(generator.calls) == 2 and generator.calls[1][1]

    generator = StubGenerator([{"decision": "query", "sql": "DELETE FROM policies"}] * 3)
    result = asyncio.run(answer_question(None, generator, "删除"))
    assert result.status == "failed" and len(result.attempts) == 3


def test_actual_reader_permissions_and_quarterly_metrics():
    url = os.environ.get("M3_TEST_READER_DATABASE_URL")
    if not url:
        pytest.skip("set M3_TEST_READER_DATABASE_URL for real PostgreSQL M3 checks")

    async def check():
        engine = create_async_engine(url)
        try:
            schema = await schema_context(engine)
            assert "policies(" in schema and "claim_payments(" in schema
            assert "claims.policy_id -> policies.id" in schema
            assert await execute_readonly(engine, "SELECT count(*) AS n FROM policies") == [{"n": 30000}]
            rows = await period_summary(engine, date(2026, 4, 1), date(2026, 7, 1),
                                        region="south")
            assert len(rows) == 12 and all(int(row["exposure_days"]) > 0 for row in rows)
            product_rows = await period_summary(engine, date(2026, 4, 1),
                                                date(2026, 7, 1), product_code="product_001")
            assert len(product_rows) == 4
            incurred = sum(Decimal(row["incurred_amount"]) for row in product_rows)
            earned = sum(Decimal(row["earned_premium"]) for row in product_rows)
            ratio = (incurred / earned).quantize(Decimal("0.0001"), ROUND_HALF_UP)
            casebook = yaml.safe_load(Path("evaluation/sql/cases.yaml").read_text(encoding="utf-8"))
            gold = next(case for case in casebook["cases"]
                        if case["id"] == "q2_loss_ratio_001")
            assert str(ratio) == gold["expected_result"][0]["incurred_loss_ratio"]
            async with engine.connect() as conn:
                username = (await conn.execute(text("SELECT current_user"))).scalar_one()
                assert username == "insurance_reader"
                flags = (await conn.execute(text(
                    "SELECT has_table_privilege(current_user,'policies','SELECT'),"
                    "has_table_privilege(current_user,'policies','INSERT'),"
                    "has_table_privilege(current_user,'policies','UPDATE'),"
                    "has_table_privilege(current_user,'policies','DELETE')"
                ))).one()
                assert tuple(flags) == (True, False, False, False)
                with pytest.raises(Exception):
                    await conn.execute(text("INSERT INTO policies(id) VALUES (-1)"))
            with pytest.raises(ValueError, match="exceeds"):
                await execute_readonly(engine, "SELECT id FROM policies ORDER BY id", max_rows=3)
        finally:
            await engine.dispose()

    asyncio.run(check())
