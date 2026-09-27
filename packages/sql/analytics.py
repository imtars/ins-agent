"""Deterministic synthetic insurance measures; never ask an LLM to calculate them."""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.ext.asyncio import AsyncEngine

from packages.sql.runtime import execute_readonly

CENT = Decimal("0.01")
RATIO = Decimal("0.0001")


def period_sql(start: date, end: date, *, product_code: str | None = None,
               region: str | None = None) -> str:
    if start >= end or (end - start).days > 366:
        raise ValueError("period must be a positive interval of at most 366 days")
    if product_code is not None and (len(product_code) != 11 or not product_code.startswith("product_")
                                     or not product_code[-3:].isdigit()):
        raise ValueError("invalid product code")
    if region is not None and region not in ("north", "south", "east", "west"):
        raise ValueError("invalid region")
    filters = []
    if product_code:
        filters.append(f"d.product_code = '{product_code}'")
    if region:
        filters.append(f"b.region = '{region}'")
    where = " AND ".join(filters) if filters else "TRUE"
    left, right = start.isoformat(), end.isoformat()
    # [start,end) and [policy.start_date,policy.end_date) avoid boundary double counts.
    return f"""
WITH exposure AS (
  SELECT p.id AS policy_id, d.product_code, b.region,
    GREATEST(0, LEAST(p.end_date, DATE '{right}') -
                GREATEST(p.start_date, DATE '{left}')) AS in_force_days,
    p.annual_premium
  FROM policies p JOIN products d ON p.product_id=d.id
    JOIN branches b ON p.branch_id=b.id
  WHERE {where}
), premium AS (
  SELECT product_code, region,
    SUM(in_force_days) AS exposure_days,
    SUM(annual_premium * in_force_days / 365) AS earned_premium
  FROM exposure WHERE in_force_days>0 GROUP BY product_code, region
), incurred AS (
  SELECT e.product_code, e.region, COUNT(*) AS claim_count,
    SUM(c.claim_amount) AS incurred_amount
  FROM exposure e JOIN claims c ON c.policy_id=e.policy_id
  WHERE c.claim_date >= DATE '{left}' AND c.claim_date < DATE '{right}'
    AND c.status <> 'denied'
  GROUP BY e.product_code,e.region
), paid AS (
  SELECT e.product_code, e.region, SUM(cp.amount) AS paid_amount
  FROM exposure e JOIN claims c ON c.policy_id=e.policy_id
    JOIN claim_payments cp ON cp.claim_id=c.id
  WHERE cp.payment_date >= DATE '{left}' AND cp.payment_date < DATE '{right}'
  GROUP BY e.product_code,e.region
)
SELECT p.product_code,p.region,p.exposure_days,p.earned_premium,
  COALESCE(i.claim_count,0) AS claim_count,
  COALESCE(i.incurred_amount,0) AS incurred_amount,
  COALESCE(a.paid_amount,0) AS paid_amount
FROM premium p LEFT JOIN incurred i USING(product_code,region)
  LEFT JOIN paid a USING(product_code,region)
ORDER BY p.product_code,p.region
""".strip()


def measures(row: dict) -> dict:
    earned = Decimal(str(row["earned_premium"]))
    incurred = Decimal(str(row["incurred_amount"]))
    paid = Decimal(str(row["paid_amount"]))
    years = Decimal(row["exposure_days"]) / Decimal(365)
    return {"product_code": row["product_code"], "region": row["region"],
            "exposure_days": row["exposure_days"],
            "in_force_policy_years": str(years.quantize(RATIO, ROUND_HALF_UP)),
            "earned_premium": str(earned.quantize(CENT, ROUND_HALF_UP)),
            "claim_count": row["claim_count"],
            "incurred_amount": str(incurred.quantize(CENT, ROUND_HALF_UP)),
            "paid_amount": str(paid.quantize(CENT, ROUND_HALF_UP)),
            "incurred_loss_ratio": (str((incurred / earned).quantize(RATIO, ROUND_HALF_UP))
                                    if earned else None),
            "paid_loss_ratio": (str((paid / earned).quantize(RATIO, ROUND_HALF_UP))
                                if earned else None),
            "claims_per_in_force_policy_year": (
                str((Decimal(row["claim_count"]) / years).quantize(RATIO, ROUND_HALF_UP))
                if years else None)}


async def period_summary(engine: AsyncEngine, start: date, end: date, **filters) -> list[dict]:
    return [measures(row) for row in await execute_readonly(
        engine, period_sql(start, end, **filters), max_rows=200)]
