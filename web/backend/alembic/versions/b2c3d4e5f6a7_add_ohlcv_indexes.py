"""add_ohlcv_indexes

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-03 18:00:00

Phase 2: Market Data Service — OHLCV Index Optimization
Adds 2 indexes to the ohlcv table:
  - idx_ohlcv_timestamp: (timestamp) — retention pruning queries
  - idx_ohlcv_asset_tf_latest: (asset_id, timeframe, timestamp DESC) — "latest N bars" cold-start reads
No new tables, no new columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_ohlcv_timestamp", "ohlcv", ["timestamp"])
    op.create_index(
        "idx_ohlcv_asset_tf_latest",
        "ohlcv",
        ["asset_id", "timeframe", sa.text("timestamp DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_ohlcv_asset_tf_latest")
    op.drop_index("idx_ohlcv_timestamp")
