"""editable dynamic staffing config (interval/target_utilization/lookahead)

Revision ID: fbe6735fde6d
Revises: f3b8e2c6a1d4
Create Date: 2026-09-28 11:00:00.000000

ADIM (MEGA Base Revision + SQL-Editable Dynamic Config) - kullanıcı
talebi: phpMyAdmin'den (1) passport_dep/passport_arr için AYRI checkpoint
aralığı, (2) dynamic hedef utilization, (3) passport_arr proaktif
lookahead penceresi görülüp değiştirilebilsin. `base`/`max` (departure_
passport_servers[_max], international_security_lanes[_max]) ve ORTAK
security interval (security_dynamic_control_interval_minutes) zaten
önceki migration'larda (e2a7c4f1d9b6) eklenmişti - bu migration SADECE
eksik kalan, process-özel/global dynamic parametreleri ekliyor.

`allowed_levels` (20/25/30/35/40 vb.) için AYRI bir kolon AÇILMADI -
bunlar `base`/`max`/sabit step=5'ten runtime'da türetiliyor (bkz.
engine.py `_operational_levels_for`) - "gereksiz duplicate kolon
üretme" ilkesi.

Additive ALTER TABLE ... ADD COLUMN - mevcut veri/kolon silinmiyor,
hiçbir prediction/wait/FIFO matematiği bu migration'ın KENDİSİYLE
değişmiyor (davranış değişikliği ayrı, kod tarafındaki okuma
mantığından gelir).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fbe6735fde6d'
down_revision: Union[str, Sequence[str], None] = 'f3b8e2c6a1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('airport_scale_configs', sa.Column('passport_departure_control_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('airport_scale_configs', sa.Column('passport_arrival_control_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('airport_scale_configs', sa.Column('dynamic_target_utilization', sa.Float(), nullable=True))
    op.add_column('airport_scale_configs', sa.Column('passport_arrival_lookahead_minutes', sa.Integer(), nullable=True))

    op.add_column('queue_resource_config_audit', sa.Column('passport_departure_control_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('passport_arrival_control_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('dynamic_target_utilization', sa.Float(), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('passport_arrival_lookahead_minutes', sa.Integer(), nullable=True))

    op.add_column('queue_dynamic_staffing_audit', sa.Column('base_resource_count', sa.Integer(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('max_resource_count', sa.Integer(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('total_workload', sa.Float(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('control_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('service_time_minutes', sa.Float(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('target_utilization', sa.Float(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('effective_capacity_per_minute', sa.Float(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('effective_capacity_per_hour', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('queue_dynamic_staffing_audit', 'effective_capacity_per_hour')
    op.drop_column('queue_dynamic_staffing_audit', 'effective_capacity_per_minute')
    op.drop_column('queue_dynamic_staffing_audit', 'target_utilization')
    op.drop_column('queue_dynamic_staffing_audit', 'service_time_minutes')
    op.drop_column('queue_dynamic_staffing_audit', 'control_interval_minutes')
    op.drop_column('queue_dynamic_staffing_audit', 'total_workload')
    op.drop_column('queue_dynamic_staffing_audit', 'max_resource_count')
    op.drop_column('queue_dynamic_staffing_audit', 'base_resource_count')

    op.drop_column('queue_resource_config_audit', 'passport_arrival_lookahead_minutes')
    op.drop_column('queue_resource_config_audit', 'dynamic_target_utilization')
    op.drop_column('queue_resource_config_audit', 'passport_arrival_control_interval_minutes')
    op.drop_column('queue_resource_config_audit', 'passport_departure_control_interval_minutes')

    op.drop_column('airport_scale_configs', 'passport_arrival_lookahead_minutes')
    op.drop_column('airport_scale_configs', 'dynamic_target_utilization')
    op.drop_column('airport_scale_configs', 'passport_arrival_control_interval_minutes')
    op.drop_column('airport_scale_configs', 'passport_departure_control_interval_minutes')
