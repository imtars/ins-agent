"""Create synthetic insurance operational schema.

Revision ID: 20260927_01
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "20260927_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("branch_code", sa.String(16), nullable=False, unique=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("region", sa.String(20), nullable=False),
        sa.CheckConstraint("region IN ('north','south','east','west')", name="branch_region_valid"),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_code", sa.String(16), nullable=False, unique=True),
        sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=False),
        sa.Column("joined_on", sa.Date(), nullable=False),
    )
    op.create_index("ix_agents_branch_id", "agents", ["branch_id"])
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_code", sa.String(20), nullable=False, unique=True),
        sa.Column("home_branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("risk_score", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("risk_score BETWEEN 0 AND 100", name="customer_risk_score_range"),
    )
    op.create_index("ix_customers_home_branch_id", "customers", ["home_branch_id"])
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_code", sa.String(16), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("product_type", sa.String(20), nullable=False),
        sa.Column("annual_base_premium", sa.Numeric(12, 2), nullable=False),
        sa.Column("claim_frequency_factor", sa.Numeric(5, 3), nullable=False),
        sa.Column("claim_severity_factor", sa.Numeric(5, 3), nullable=False),
        sa.CheckConstraint("product_type IN ('motor','health','accident','life')", name="product_type_valid"),
        sa.CheckConstraint("annual_base_premium > 0", name="product_premium_positive"),
        sa.CheckConstraint("claim_frequency_factor > 0", name="product_frequency_positive"),
        sa.CheckConstraint("claim_severity_factor > 0", name="product_severity_positive"),
    )
    op.create_table(
        "policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("policy_code", sa.String(20), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("annual_premium", sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint("end_date > start_date", name="policy_dates_ordered"),
        sa.CheckConstraint("annual_premium > 0", name="policy_premium_positive"),
    )
    for col in ("customer_id", "agent_id", "branch_id", "product_id", "start_date"):
        op.create_index(f"ix_policies_{col}", "policies", [col])
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("claim_code", sa.String(20), nullable=False, unique=True),
        sa.Column("policy_id", sa.Integer(), sa.ForeignKey("policies.id"), nullable=False, unique=True),
        sa.Column("claim_date", sa.Date(), nullable=False),
        sa.Column("claim_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint("claim_amount > 0", name="claim_amount_positive"),
        sa.CheckConstraint("status IN ('settled','open','denied')", name="claim_status_valid"),
    )
    op.create_index("ix_claims_claim_date", "claims", ["claim_date"])
    op.create_table(
        "claim_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("claim_id", sa.Integer(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint("amount > 0", name="payment_amount_positive"),
    )
    op.create_index("ix_claim_payments_claim_id", "claim_payments", ["claim_id"])


def downgrade() -> None:
    op.drop_table("claim_payments")
    op.drop_table("claims")
    op.drop_table("policies")
    op.drop_table("products")
    op.drop_table("customers")
    op.drop_table("agents")
    op.drop_table("branches")
