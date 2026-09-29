"""airport departure passenger flow (non-queue congestion metric)

Revision ID: d9e2a4f6b8c1
Revises: b4d8f1c7e3a2
Create Date: 2026-09-29 14:00:00.000000

ADIM (Airport Departure Passenger Flow) - kullanıcı talebi: bir
havalimanındaki TÜM departure uçuşlarının yolcularının havalimanına
GELİŞ akışını (show-up flow) 5dk/30dk/saatlik görmek - bu QUEUE WAIT
DEĞİL (security/passport/backlog/remaining-wait'e hiç dokunulmuyor).

`queue_airport_departure_flow_5m`, `queue_cohort_audit`'teki (direction=
'departure') cohort satırlarının process bazında (security_dom=domestic,
security_intl=schengen-direct, passport_dep=non-schengen) TEK bir ek
geçişte toplanmasıdır - farklı bir yolcu-üretim formülü YOK, `departure_
show_up_events_detailed()`'in ZATEN ürettiği (cohort_time, count)
çiftleri re-aggregate ediliyor (bkz. audit.py:record_flight_cohort_and_
contribution_audit). Arrival flight'lar dahil değil.

Additive CREATE TABLE - mevcut queue/FIFO/dynamic-staffing/security/
passport matematiği bu migration'ın KENDİSİYLE değişmiyor.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9e2a4f6b8c1'
down_revision: Union[str, Sequence[str], None] = 'b4d8f1c7e3a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'queue_airport_departure_flow_5m',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('window_start_utc', sa.DateTime(), nullable=False),
        sa.Column('window_start_local', sa.DateTime(), nullable=True),
        sa.Column('window_end_utc', sa.DateTime(), nullable=False),
        sa.Column('window_end_local', sa.DateTime(), nullable=True),
        sa.Column('total_departure_pax', sa.Float(), nullable=False),
        sa.Column('domestic_departure_pax', sa.Float(), nullable=False),
        sa.Column('international_departure_pax', sa.Float(), nullable=False),
        sa.Column('schengen_departure_pax', sa.Float(), nullable=False),
        sa.Column('non_schengen_departure_pax', sa.Float(), nullable=False),
        sa.Column('flight_count', sa.Integer(), nullable=False),
        sa.Column('calculation_method', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('run_id', 'airport_iata', 'window_start_utc', name='uq_queue_airport_departure_flow_5m'),
    )
    op.create_index('ix_queue_airport_departure_flow_5m_run_id', 'queue_airport_departure_flow_5m', ['run_id'])
    op.create_index('ix_queue_airport_departure_flow_5m_airport_iata', 'queue_airport_departure_flow_5m', ['airport_iata'])
    op.create_index('ix_queue_airport_departure_flow_5m_window_start_utc', 'queue_airport_departure_flow_5m', ['window_start_utc'])


def downgrade() -> None:
    op.drop_index('ix_queue_airport_departure_flow_5m_window_start_utc', table_name='queue_airport_departure_flow_5m')
    op.drop_index('ix_queue_airport_departure_flow_5m_airport_iata', table_name='queue_airport_departure_flow_5m')
    op.drop_index('ix_queue_airport_departure_flow_5m_run_id', table_name='queue_airport_departure_flow_5m')
    op.drop_table('queue_airport_departure_flow_5m')
