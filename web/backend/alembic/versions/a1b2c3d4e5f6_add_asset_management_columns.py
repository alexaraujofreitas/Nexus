"""add_asset_management_columns

Revision ID: a1b2c3d4e5f6
Revises: 00788dff2b9e
Create Date: 2026-04-03 12:00:00

Phase 1: Asset Management — Schema + API Foundation
Adds 4 columns to the assets table:
  - is_tradable (bool, default false)
  - allocation_weight (float, default 1.0)
  - market_snapshot (JSONB, nullable — Phase 2-owned)
  - snapshot_updated_at (timestamptz, nullable — Phase 2-owned)
Creates partial index ix_assets_tradable on (exchange_id, is_tradable)
  WHERE is_tradable = true.
Seeds the 5 current watchlist symbols on the active exchange.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "00788dff2b9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Current production watchlist symbols and their allocation weights.
# Source: core/scanning/watchlist.py DEFAULT_WATCHLIST (symbols)
#         + CLAUDE.md Symbol Weights STATIC mode (weights).
_SEED_SYMBOLS = {
    "BTC/USDT:USDT": 1.0,
    "ETH/USDT:USDT": 1.2,
    "SOL/USDT:USDT": 1.3,
    "BNB/USDT:USDT": 0.8,
    "XRP/USDT:USDT": 0.8,
}


def upgrade() -> None:
    # Step 1: Add columns with server defaults
    op.add_column(
        "assets",
        sa.Column("is_tradable", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )
    op.add_column(
        "assets",
        sa.Column("allocation_weight", sa.Float(), nullable=False,
                  server_default=sa.text("1.0")),
    )
    op.add_column(
        "assets",
        sa.Column("market_snapshot", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "assets",
        sa.Column("snapshot_updated_at", sa.DateTime(timezone=True),
                  nullable=True),
    )

    # Step 2: Partial index — used by GET /exchanges/{id}/assets/tradable
    op.create_index(
        "ix_assets_tradable",
        "assets",
        ["exchange_id", "is_tradable"],
        postgresql_where=sa.text("is_tradable = true"),
    )

    # Step 3: Seed current watchlist as tradable on the active exchange.
    # If no active exchange or no matching assets rows exist, this is a
    # safe no-op (UPDATE ... WHERE ... matches zero rows).
    for symbol, weight in _SEED_SYMBOLS.items():
        op.execute(
            sa.text(
                """
                UPDATE assets
                SET is_tradable = true, allocation_weight = :weight
                WHERE symbol = :symbol
                  AND exchange_id = (
                      SELECT id FROM exchanges
                      WHERE is_active = true
                      LIMIT 1
                  )
                """
            ).bindparams(symbol=symbol, weight=weight)
        )


def downgrade() -> None:
    op.drop_index("ix_assets_tradable", table_name="assets")
    op.drop_column("assets", "snapshot_updated_at")
    op.drop_column("assets", "market_snapshot")
    op.drop_column("assets", "allocation_weight")
    op.drop_column("assets", "is_tradable")
