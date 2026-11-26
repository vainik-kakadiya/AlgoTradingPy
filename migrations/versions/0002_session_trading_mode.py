"""paper_sessions.trading_mode: PAPER (simulated) or LIVE (real orders on Zerodha)

Revision ID: 0002_trading_mode
Revises: 0001_initial
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002_trading_mode'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every existing session was simulated.
    op.add_column('paper_sessions', sa.Column('trading_mode', sa.String(length=10), nullable=False, server_default='PAPER'))


def downgrade() -> None:
    with op.batch_alter_table('paper_sessions') as batch:
        batch.drop_column('trading_mode')
