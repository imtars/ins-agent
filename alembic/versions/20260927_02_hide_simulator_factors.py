"""Remove simulator-only factors from the operational products table.

Revision ID: 20260927_02
Revises: 20260927_01
"""

from alembic import op
import sqlalchemy as sa

revision = "20260927_02"
down_revision = "20260927_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("product_frequency_positive", "products", type_="check")
    op.drop_constraint("product_severity_positive", "products", type_="check")
    op.drop_column("products", "claim_frequency_factor")
    op.drop_column("products", "claim_severity_factor")


def downgrade() -> None:
    # Original simulator values cannot be recovered from the business table.
    op.add_column("products", sa.Column("claim_frequency_factor", sa.Numeric(5, 3),
                                        nullable=False, server_default="1.000"))
    op.add_column("products", sa.Column("claim_severity_factor", sa.Numeric(5, 3),
                                        nullable=False, server_default="1.000"))
    op.create_check_constraint("product_frequency_positive", "products",
                               "claim_frequency_factor > 0")
    op.create_check_constraint("product_severity_positive", "products",
                               "claim_severity_factor > 0")
    op.alter_column("products", "claim_frequency_factor", server_default=None)
    op.alter_column("products", "claim_severity_factor", server_default=None)
