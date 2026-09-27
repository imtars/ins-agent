"""mcp-data: expose accepted SQL and analytics functions through typed tools."""

from datetime import date
import os
from typing import Annotated, Literal, TypedDict

from fastmcp import FastMCP
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from packages.sql.analytics import (GROWTH_METRICS, grouped_period_summary,
                                    period_growth)
from packages.sql.runtime import execute_readonly, require_reader, schema_context
from packages.sql.security import BUSINESS_TABLES

Region = Literal["north", "south", "east", "west"]
Group = Literal["all", "product_code", "region"]
GrowthMetric = Literal["earned_premium", "incurred_amount", "paid_amount",
                       "claim_count", "exposure_days"]
RatioKind = Literal["incurred", "paid"]


class SchemaResult(TypedDict):
    schema: str


class QueryRowsResult(TypedDict):
    rows: list[dict]
    row_count: int


class TableSampleResult(QueryRowsResult):
    table: str


class ClaimRateResult(TypedDict):
    metric: str
    value: str | None
    unit: str
    exposure_basis: str
    start_date: str
    end_date: str
    product_code: str | None
    region: str | None
    claim_count: int
    in_force_policy_years: str


class LossRatioResult(TypedDict):
    metric: str
    value: str | None
    unit: str
    start_date: str
    end_date: str
    product_code: str | None
    region: str | None
    incurred_amount: str
    paid_amount: str
    earned_premium: str


class GrowthResult(TypedDict):
    metric: str
    previous: str
    current: str
    growth_rate: str | None
    previous_start: str
    previous_end: str
    current_start: str
    current_end: str
    product_code: str | None
    region: str | None
    unit: str


class StatisticsResult(TypedDict):
    group_by: str
    start_date: str
    end_date: str
    product_code: str | None
    region: str | None
    rows: list[dict]
    row_count: int


def create_server(engine: AsyncEngine) -> FastMCP:
    require_reader(engine)
    mcp = FastMCP("mcp-data", instructions=(
        "Read-only synthetic insurance operations. All dates use ISO format; end dates "
        "are exclusive. Rates are decimal ratios, not percentages."))

    @mcp.tool
    async def describe_schema() -> SchemaResult:
        """Return the live business schema, foreign keys, and stored value meanings."""
        return {"schema": await schema_context(engine)}

    @mcp.tool
    async def execute_readonly_query(sql: str) -> QueryRowsResult:
        """Run one allowlisted SELECT using the reader role, 5s timeout, and 200-row cap."""
        rows = await execute_readonly(engine, sql)
        return {"rows": rows, "row_count": len(rows)}

    @mcp.tool
    async def get_table_sample(table: str, limit: Annotated[int, Field(ge=1, le=20)] = 5) -> TableSampleResult:
        """Return the first rows of one allowlisted business table, ordered by id."""
        if table not in BUSINESS_TABLES:
            raise ValueError("table is not an allowlisted business table")
        rows = await execute_readonly(engine, f"SELECT * FROM {table} ORDER BY id LIMIT {limit}")
        return {"table": table, "rows": rows, "row_count": len(rows)}

    @mcp.tool
    async def compute_claim_rate(start_date: date, end_date: date,
                                 product_code: str | None = None,
                                 region: Region | None = None) -> ClaimRateResult:
        """Claims divided by in-force policy years, without waiting-period adjustment."""
        rows = await grouped_period_summary(engine, start_date, end_date,
                                            product_code=product_code, region=region)
        if not rows:
            raise ValueError("no in-force exposure for the selected period and filters")
        summary = rows[0]
        return {"metric": "claims_per_in_force_policy_year", "value": summary[
            "claims_per_in_force_policy_year"], "unit": "claims_per_in_force_policy_year",
            "exposure_basis": "in_force_days_not_waiting_period_adjusted",
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "product_code": product_code, "region": region,
            "claim_count": summary["claim_count"],
            "in_force_policy_years": summary["in_force_policy_years"]}

    @mcp.tool
    async def compute_loss_ratio(start_date: date, end_date: date,
                                 kind: RatioKind = "incurred",
                                 product_code: str | None = None,
                                 region: Region | None = None) -> LossRatioResult:
        """Return incurred or paid claims divided by earned premium as a decimal ratio."""
        rows = await grouped_period_summary(engine, start_date, end_date,
                                            product_code=product_code, region=region)
        if not rows:
            raise ValueError("no in-force exposure for the selected period and filters")
        summary = rows[0]
        return {"metric": f"{kind}_loss_ratio", "value": summary[f"{kind}_loss_ratio"],
                "unit": "ratio", "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(), "product_code": product_code,
                "region": region, "incurred_amount": summary["incurred_amount"],
                "paid_amount": summary["paid_amount"],
                "earned_premium": summary["earned_premium"]}

    @mcp.tool
    async def compute_growth(previous_start: date, previous_end: date,
                             current_start: date, current_end: date,
                             metric: GrowthMetric = "earned_premium",
                             product_code: str | None = None,
                             region: Region | None = None) -> GrowthResult:
        """Compare a metric over non-overlapping equal-length periods; null if base is zero."""
        if (previous_start >= previous_end or current_start >= current_end
                or previous_end > current_start
                or previous_end - previous_start != current_end - current_start):
            raise ValueError("growth requires non-overlapping equal-length positive periods")
        if metric not in GROWTH_METRICS:
            raise ValueError("unsupported growth metric")
        previous = await grouped_period_summary(engine, previous_start, previous_end,
                                                product_code=product_code, region=region)
        current = await grouped_period_summary(engine, current_start, current_end,
                                               product_code=product_code, region=region)
        if not previous or not current:
            raise ValueError("no in-force exposure in one of the selected periods")
        return {**period_growth(previous[0], current[0], metric),
                "previous_start": previous_start.isoformat(),
                "previous_end": previous_end.isoformat(),
                "current_start": current_start.isoformat(),
                "current_end": current_end.isoformat(),
                "product_code": product_code, "region": region,
                "unit": "ratio"}

    @mcp.tool
    async def group_statistics(start_date: date, end_date: date,
                               group_by: Group = "product_code",
                               product_code: str | None = None,
                               region: Region | None = None) -> StatisticsResult:
        """Return deterministic exposure, premium, claim, payment, and ratio groups."""
        rows = await grouped_period_summary(engine, start_date, end_date,
                                            group_by=group_by, product_code=product_code,
                                            region=region)
        return {"group_by": group_by, "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(), "product_code": product_code,
                "region": region, "rows": rows, "row_count": len(rows)}

    return mcp


def main() -> None:
    url = os.environ.get("M3_READER_DATABASE_URL")
    if not url:
        raise ValueError("M3_READER_DATABASE_URL is required for mcp-data")
    engine = create_async_engine(url)
    create_server(engine).run(transport="stdio")


if __name__ == "__main__":
    main()
