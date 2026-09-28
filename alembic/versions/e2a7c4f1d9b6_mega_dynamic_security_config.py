"""mega dynamic security config

Revision ID: e2a7c4f1d9b6
Revises: d1e5f6a2b8c4
Create Date: 2026-09-28 02:00:00.000000

MEGA airportlarda international security (security_intl) artık
passport_dep/passport_arr gibi dynamic resource scaling'e giriyor
(source of truth: base=15, max=40, 5-minute control interval).
Service math (1 lane/desk = 1 passenger/minute) DEĞİŞMEDİ - bu sadece
yeni resource-opening policy alanlarını taşıyan additive kolonlar:

  - airport_scale_configs.international_security_lanes_max: security
    dynamic'in tavanı. Var olan `international_security_lanes` kolonu
    BASE olarak yeniden yorumlanıyor (ayrı bir "_base" kolonu AÇILMADI -
    zaten o rolü oynuyordu). NULL kalan scale'ler (LARGE/MEDIUM/SMALL)
    için security STATIC kalmaya devam eder.
  - airport_scale_configs.security_dynamic_control_interval_minutes /
    passport_dynamic_control_interval_minutes: checkpoint aralığı
    artık DB'den okunuyor (eskiden passport'ta hardcoded 10dk idi).
    NULL ise kod tarafında 5 dakikaya düşülür.
  - queue_resource_config_audit: aynı yeni alanların audit'te
    görülebilmesi için international_security_lanes_max,
    security_control_interval_minutes, passport_control_interval_minutes.
  - queue_dynamic_staffing_audit.reason: her checkpoint için okunabilir
    sınıflandırma (normal_load/demand_threshold/backlog_pressure/
    lookahead_pressure/peak_pressure/scale_down).

Sadece additive ALTER TABLE ... ADD COLUMN - mevcut veri/kolon
silinmiyor, hiçbir prediction/wait/FIFO matematiği etkilenmiyor.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2a7c4f1d9b6'
down_revision: Union[str, Sequence[str], None] = 'd1e5f6a2b8c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('airport_scale_configs', sa.Column('international_security_lanes_max', sa.Integer(), nullable=True))
    op.add_column('airport_scale_configs', sa.Column('security_dynamic_control_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('airport_scale_configs', sa.Column('passport_dynamic_control_interval_minutes', sa.Integer(), nullable=True))

    op.add_column('queue_resource_config_audit', sa.Column('international_security_lanes_max', sa.Integer(), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('security_control_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('passport_control_interval_minutes', sa.Integer(), nullable=True))

    op.add_column('queue_dynamic_staffing_audit', sa.Column('reason', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('queue_dynamic_staffing_audit', 'reason')

    op.drop_column('queue_resource_config_audit', 'passport_control_interval_minutes')
    op.drop_column('queue_resource_config_audit', 'security_control_interval_minutes')
    op.drop_column('queue_resource_config_audit', 'international_security_lanes_max')

    op.drop_column('airport_scale_configs', 'passport_dynamic_control_interval_minutes')
    op.drop_column('airport_scale_configs', 'security_dynamic_control_interval_minutes')
    op.drop_column('airport_scale_configs', 'international_security_lanes_max')
