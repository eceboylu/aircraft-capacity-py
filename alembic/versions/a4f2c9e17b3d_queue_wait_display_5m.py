"""queue wait display 5m

Revision ID: a4f2c9e17b3d
Revises: bc083daf562b
Create Date: 2026-09-25 21:30:00.000000

ADIM (5-Minute Wait Display Series) - `app/queue/models.py:
QueueWaitDisplay5m` için tablo. Saatlik `queue_predictions` tablosuna
DOKUNMAZ - tamamen ayrı, SADECE grafik görüntüleme çözünürlüğü için
ek bir tablo (bkz. model docstring'i). Şema bilinçli olarak DAR:
yolcu sayısı/utilization/backlog burada YOK, sadece wait + risk.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4f2c9e17b3d'
down_revision: Union[str, Sequence[str], None] = 'bc083daf562b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'queue_wait_display_5m',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('window_start', sa.DateTime(), nullable=False),
        sa.Column('estimated_wait_minutes', sa.Float(), nullable=False),
        sa.Column('risk', sa.String(length=16), nullable=False),
        sa.Column('calculated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'airport_iata', 'process', 'window_start',
            name='uq_queue_wait_display_5m_window',
        ),
    )
    op.create_index(
        op.f('ix_queue_wait_display_5m_airport_iata'),
        'queue_wait_display_5m', ['airport_iata'], unique=False,
    )
    op.create_index(
        op.f('ix_queue_wait_display_5m_window_start'),
        'queue_wait_display_5m', ['window_start'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_queue_wait_display_5m_window_start'), table_name='queue_wait_display_5m')
    op.drop_index(op.f('ix_queue_wait_display_5m_airport_iata'), table_name='queue_wait_display_5m')
    op.drop_table('queue_wait_display_5m')
