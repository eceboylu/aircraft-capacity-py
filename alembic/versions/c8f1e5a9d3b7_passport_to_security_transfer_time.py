"""passport to security transfer time (lineage column)

Revision ID: c8f1e5a9d3b7
Revises: d9e2a4f6b8c1
Create Date: 2026-09-30 12:00:00.000000

ADIM (Passport -> Security Transfer Time) - kullanıcı kararı: non-Schengen
international departure yolcusu passport_dep completion'dan security_intl
arrival'a geçerken sabit 5 dakikalık transfer/walking-time offset'i
uygulanıyor (OPTION A - event-level, her passport completion event'i
KENDİ exact timestamp'ini korur, batching YOK - bkz. engine.py
`PASSPORT_TO_SECURITY_TRANSFER_MINUTES`, `_event_driven_queue_demand`).

Bu migration SADECE `queue_resource_config_audit`'e bu offset'in o run'da
ne olduğunu gösteren bir GÖSTERİM/lineage kolonu ekliyor - hesaplamanın
KENDİSİ bu migration'la DEĞİŞMİYOR (Python sabiti zaten `engine.py`'de).
Additive ALTER TABLE - mevcut queue/FIFO/dynamic-staffing/security/
passport/arrival matematiği bu migration'ın KENDİSİYLE değişmiyor.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f1e5a9d3b7'
down_revision: Union[str, None] = 'd9e2a4f6b8c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'queue_resource_config_audit',
        sa.Column('passport_to_security_transfer_minutes', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('queue_resource_config_audit', 'passport_to_security_transfer_minutes')
