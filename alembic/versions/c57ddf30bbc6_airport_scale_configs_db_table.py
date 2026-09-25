"""airport scale configs db table

Revision ID: c57ddf30bbc6
Revises: 071a819fe394
Create Date: 2026-09-25 13:20:00.000000

ADIM (DB-Editable Scale Resource Contract) - `app/queue/models.py:
AirportScaleConfig` için tablo. `airport_scale_configs` bu isimle
ÖNCEDEN de var olabilir (bu ADIM'dan önceki, kodun hiç okumadığı bir
yetim/orphan tablo olarak - eski 3-tier değerleri taşıyordu, mega hiç
yoktu). Bu migration İKİ senaryoyu da doğru ele alır:

  FRESH DB (tablo hiç yok):
      `upgrade()` tabloyu SIFIRDAN `op.create_table()` ile kurar.

  MEVCUT DB (tablo zaten var, orphan haliyle):
      Bu host için `upgrade()` ÇALIŞTIRILMAZ - bunun yerine (aynı
      `fc0ecac68804`/`071a819fe394` reconciliation'ında kullanılan
      desenle) `alembic stamp c57ddf30bbc6` ile SADECE alembic_version
      ilerletilir; şemaya HİÇ DOKUNULMAZ (tablo zaten doğru şekilde
      mevcuttur). `downgrade()` orphan/pre-existing tabloyu SİLMEZ -
      bu migration'ın SORUMLULUĞUNDA olan durum SADECE "fresh DB'de
      tablo yok" senaryosudur.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c57ddf30bbc6'
down_revision: Union[str, Sequence[str], None] = '071a819fe394'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'airport_scale_configs',
        sa.Column('scale', sa.String(length=16), nullable=False),
        sa.Column('departure_passport_servers', sa.Integer(), nullable=False),
        sa.Column('departure_passport_servers_max', sa.Integer(), nullable=True),
        sa.Column('arrival_passport_servers', sa.Integer(), nullable=False),
        sa.Column('arrival_passport_servers_max', sa.Integer(), nullable=True),
        sa.Column('domestic_security_lanes', sa.Integer(), nullable=False),
        sa.Column('international_security_lanes', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('scale'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('airport_scale_configs')
