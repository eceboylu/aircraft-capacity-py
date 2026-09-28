"""dynamic operational levels

Revision ID: f3b8e2c6a1d4
Revises: e2a7c4f1d9b6
Create Date: 2026-09-28 08:00:00.000000

MEGA dynamic resource'lar (security_intl, passport_dep, passport_arr)
artık rastgele bir tamsayıya değil, sadece sabit operational level
listelerinden birine oturuyor (15/20/25/30/35/40, passport_arr için
30/35/40). Bu, sadece bir audit KOLONU ekliyor -
`queue_dynamic_staffing_audit.target_operational_level`: "gereken sayı"
(needed_servers, ham matematik) ile "uygulanan seviye" (yeni
new_server_count, tek-adım kısıtlı) artık ayrı ayrı görülebiliyor.
Additive - mevcut veri/kolon silinmiyor.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3b8e2c6a1d4'
down_revision: Union[str, Sequence[str], None] = 'e2a7c4f1d9b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('queue_dynamic_staffing_audit', sa.Column('target_operational_level', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('queue_dynamic_staffing_audit', 'target_operational_level')
