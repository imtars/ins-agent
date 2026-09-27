"""M4 data tools are exercised through the actual FastMCP client protocol."""

import asyncio
from decimal import Decimal
import os

from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from packages.sql.analytics import group_period_rows, period_growth
from services.mcp_data.server import create_server

DATA_TOOLS = {"describe_schema", "execute_readonly_query", "get_table_sample",
              "compute_claim_rate", "compute_loss_ratio", "compute_growth",
              "group_statistics"}


def test_grouped_analytics_use_aggregate_denominators():
    rows = [
        {"product_code": "product_001", "region": "north", "exposure_days": 365,
         "earned_premium": "100.004", "claim_count": 1,
         "incurred_amount": "20", "paid_amount": "10"},
        {"product_code": "product_001", "region": "south", "exposure_days": 365,
         "earned_premium": "99.996", "claim_count": 3,
         "incurred_amount": "80", "paid_amount": "50"},
    ]
    summary = group_period_rows(rows, "all")
    assert len(summary) == 1
    assert summary[0]["earned_premium"] == "200.00"
    assert summary[0]["incurred_loss_ratio"] == "0.5000"
    assert summary[0]["claims_per_in_force_policy_year"] == "2.0000"
    assert period_growth({"earned_premium": "0"}, summary[0],
                         "earned_premium")["growth_rate"] is None
    with pytest.raises(ValueError, match="group_by"):
        group_period_rows(rows, "invalid")


def test_data_tool_contracts_against_reader_database():
    url = os.environ.get("M3_TEST_READER_DATABASE_URL")
    if not url:
        pytest.skip("set M3_TEST_READER_DATABASE_URL for real FastMCP data contracts")

    async def check():
        engine = create_async_engine(url)
        try:
            async with Client(create_server(engine)) as client:
                tools = {item.name: item for item in await client.list_tools()}
                assert set(tools) == DATA_TOOLS
                assert "sql" in tools["execute_readonly_query"].input_schema["required"]
                assert {"rows", "row_count"} <= set(
                    tools["execute_readonly_query"].output_schema["required"])
                assert {"value", "claim_count", "exposure_basis"} <= set(
                    tools["compute_claim_rate"].output_schema["required"])
                assert {"previous", "current", "growth_rate"} <= set(
                    tools["compute_growth"].output_schema["required"])

                schema = (await client.call_tool("describe_schema")).structured_content
                assert "北部 -> 'north'" in schema["schema"]

                query = (await client.call_tool("execute_readonly_query", {
                    "sql": "SELECT count(*) AS n FROM policies"})).structured_content
                assert query == {"rows": [{"n": 30000}], "row_count": 1}

                sample = (await client.call_tool("get_table_sample", {
                    "table": "products", "limit": 2})).structured_content
                assert sample["row_count"] == 2
                assert sample["rows"][0]["product_code"] == "product_001"

                period = {"start_date": "2026-04-01", "end_date": "2026-07-01",
                          "product_code": "product_001"}
                claim_rate = (await client.call_tool("compute_claim_rate", period)).structured_content
                assert claim_rate["claim_count"] > 0
                assert claim_rate["exposure_basis"] == "in_force_days_not_waiting_period_adjusted"
                assert Decimal(claim_rate["value"]) > 0

                loss = (await client.call_tool("compute_loss_ratio", period)).structured_content
                assert loss["metric"] == "incurred_loss_ratio"
                assert loss["value"] == "0.5392"
                paid = (await client.call_tool("compute_loss_ratio", {
                    **period, "kind": "paid"})).structured_content
                assert paid["metric"] == "paid_loss_ratio"
                assert Decimal(paid["value"]) <= Decimal(loss["value"])

                growth = (await client.call_tool("compute_growth", {
                    "previous_start": "2026-01-01", "previous_end": "2026-02-01",
                    "current_start": "2026-02-01", "current_end": "2026-03-04",
                    "metric": "earned_premium"})).structured_content
                assert growth["unit"] == "ratio"
                assert growth["growth_rate"] == "0.1531"

                groups = (await client.call_tool("group_statistics", {
                    "start_date": "2026-04-01", "end_date": "2026-07-01",
                    "group_by": "region"})).structured_content
                assert groups["row_count"] == 4
                assert {row["region"] for row in groups["rows"]} == {
                    "north", "south", "east", "west"}

                for name, args in (
                    ("execute_readonly_query", {"sql": "DELETE FROM policies"}),
                    ("get_table_sample", {"table": "pg_catalog.pg_class"}),
                    ("get_table_sample", {"table": "policies", "limit": 201}),
                    ("compute_claim_rate", {**period, "region": "北部"}),
                    ("compute_loss_ratio", {**period, "kind": "unknown"}),
                    ("compute_growth", {"previous_start": "2026-01-01",
                                        "previous_end": "2026-02-01",
                                        "current_start": "2026-02-01",
                                        "current_end": "2026-03-01"}),
                    ("group_statistics", {"start_date": "2026-04-01",
                                          "end_date": "2026-07-01",
                                          "group_by": "customer_id"}),
                ):
                    with pytest.raises(ToolError):
                        await client.call_tool(name, args)
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_data_server_requires_reader_role():
    engine = create_async_engine("postgresql+asyncpg://insurance_app:password@localhost/db")
    with pytest.raises(ValueError, match="insurance_reader"):
        create_server(engine)
    asyncio.run(engine.dispose())
