"""SQL audit/traceability expansion (flight resolution + virtual-wait trace + forecast breakdown)

Revision ID: a7c3e5f9d2b1
Revises: fbe6735fde6d
Create Date: 2026-09-29 09:00:00.000000

ADIM (SQL Audit/Traceability Genişletme) - kullanıcı talebi: "Bu sayı
nereden geldi? Formül hangi girdileri kullandı? Sonuç ne çıktı? Sonraki
aşamaya ne gönderildi?" sorularının SADECE SQL'den, run_id/AIRPORT/DATE/
HOUR/PROCESS/FLIGHT ile filtrelenerek cevaplanabilmesi için. Önce bir
GAP ANALYSIS çıkarıldı (mevcut 9 audit tablosu koda karşı doğrulandı);
bu migration SADECE gerçekten eksik kalan alanları/tabloları ekliyor:

1. `queue_flight_resolution_audit` (YENİ) - her fiziksel flight için TEK
   satır: hangi zaman damgası (scheduled/estimated/actual) kullanıldı,
   uçak kapasitesi hangi kaynaktan (`CapacityResult.source`) çözüldü,
   routing/Schengen kararı ne oldu. `queue_flight_hour_contribution_audit`
   sadece SCHEDULED zamanı gösteriyordu; production (`demand.py:
   _departure_show_up_base`/`effective_time`) GERÇEKTE actual->estimated
   ->scheduled sırasıyla düşüyordu - bu YANILTICI GAP burada + aşağıdaki
   5 ek kolonla (`queue_flight_hour_contribution_audit`) kapatıldı.

2. `queue_virtual_wait_trace_audit` (YENİ) - EN ÖNEMLİ EKSİK: `queue_
   wait_display_5m` sadece nihai wait değerini taşıyordu; STATIC (ör.
   ZRH security_intl) süreçler için 5dk backlog/server-availability/
   virtual-service-start hiç SQL'den görünmüyordu (sadece MEGA dynamic
   checkpoint'leri `queue_dynamic_staffing_audit`'te vardı). Artık HER
   süreç (static+dynamic) için `virtual_arrival_wait_detailed()`'in
   ZATEN hesapladığı ara değerler persist ediliyor.

3. `queue_dynamic_staffing_audit` +5 kolon - `lookahead_demand`/`needed_
   servers` normal(5dk)+extended(20dk, sadece passport_arr) pencerelerin
   TOPLAMINI/BÜYÜĞÜNÜ taşıyordu; hangi pencerenin karara yol açtığı artık
   AYRI (ham, toplanmamış) kolonlarla görülebiliyor.

4. `queue_resource_config_audit` +4 kolon - runtime'da türetilen
   operational level listesinin (base/max/step=5) bir GÖRÜNTÜSÜ +
   3 sürecin dynamic-enabled bayrakları, JOIN'siz okunabilsin diye.

Not (Bölüm 9, passport completion -> security_intl lineage): AYRI bir
lineage id EKLENMEDİ - `queue_service_event_audit.arrival_time_utc`
(security) = `completion_time_utc` (passport) eşleşmesi zaten GÜVENİLİR
(ServiceEvent.arrival_time simülasyon boyunca DEĞİŞMİYOR) - SQL'den
`GROUP BY`/`SUM` ile doğrulanabiliyor, yeni kolon GEREKMEDİ.

Additive ALTER TABLE / CREATE TABLE - mevcut veri/kolon silinmiyor,
hiçbir prediction/wait/FIFO matematiği bu migration'ın KENDİSİYLE
değişmiyor (audit tabloları production tarafından hiç OKUNMUYOR, bkz.
app/queue/audit.py modül docstring'i / tests/test_audit_isolation.py).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e5f9d2b1'
down_revision: Union[str, Sequence[str], None] = 'fbe6735fde6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('queue_flight_hour_contribution_audit', sa.Column('dep_time_effective_utc', sa.DateTime(), nullable=True))
    op.add_column('queue_flight_hour_contribution_audit', sa.Column('dep_time_source', sa.String(length=16), nullable=True))
    op.add_column('queue_flight_hour_contribution_audit', sa.Column('arr_time_effective_utc', sa.DateTime(), nullable=True))
    op.add_column('queue_flight_hour_contribution_audit', sa.Column('arr_time_source', sa.String(length=16), nullable=True))
    op.add_column('queue_flight_hour_contribution_audit', sa.Column('aircraft_capacity_source', sa.String(length=32), nullable=True))

    op.add_column('queue_resource_config_audit', sa.Column('allowed_levels_snapshot', sa.String(length=200), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('security_intl_dynamic_enabled', sa.Boolean(), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('passport_departure_dynamic_enabled', sa.Boolean(), nullable=True))
    op.add_column('queue_resource_config_audit', sa.Column('passport_arrival_dynamic_enabled', sa.Boolean(), nullable=True))

    op.add_column('queue_dynamic_staffing_audit', sa.Column('normal_lookahead_demand', sa.Float(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('extended_lookahead_demand', sa.Float(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('normal_needed_servers', sa.Integer(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('extended_needed_servers', sa.Integer(), nullable=True))
    op.add_column('queue_dynamic_staffing_audit', sa.Column('winning_forecast', sa.String(length=16), nullable=True))

    op.create_table(
        'queue_flight_resolution_audit',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('flight_key', sa.String(length=64), nullable=False),
        sa.Column('flight_iata', sa.String(length=16), nullable=True),
        sa.Column('airline_iata', sa.String(length=8), nullable=True),
        sa.Column('direction', sa.String(length=16), nullable=False),
        sa.Column('dep_iata', sa.String(length=10), nullable=True),
        sa.Column('arr_iata', sa.String(length=10), nullable=True),
        sa.Column('dep_scheduled_utc', sa.DateTime(), nullable=True),
        sa.Column('dep_estimated_utc', sa.DateTime(), nullable=True),
        sa.Column('dep_actual_utc', sa.DateTime(), nullable=True),
        sa.Column('dep_effective_utc', sa.DateTime(), nullable=True),
        sa.Column('dep_time_source', sa.String(length=16), nullable=True),
        sa.Column('arr_scheduled_utc', sa.DateTime(), nullable=True),
        sa.Column('arr_estimated_utc', sa.DateTime(), nullable=True),
        sa.Column('arr_actual_utc', sa.DateTime(), nullable=True),
        sa.Column('arr_effective_utc', sa.DateTime(), nullable=True),
        sa.Column('arr_time_source', sa.String(length=16), nullable=True),
        sa.Column('aircraft_icao', sa.String(length=8), nullable=True),
        sa.Column('aircraft_match_found', sa.Boolean(), nullable=True),
        sa.Column('resolved_capacity', sa.Integer(), nullable=True),
        sa.Column('capacity_source', sa.String(length=32), nullable=True),
        sa.Column('capacity_confidence', sa.String(length=16), nullable=True),
        sa.Column('is_domestic', sa.Boolean(), nullable=False),
        sa.Column('is_international', sa.Boolean(), nullable=False),
        sa.Column('is_schengen', sa.Boolean(), nullable=True),
        sa.Column('requires_passport', sa.Boolean(), nullable=True),
        sa.Column('routing_path', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('run_id', 'flight_key', name='uq_queue_flight_resolution_audit'),
    )
    op.create_index('ix_queue_flight_resolution_audit_run_id', 'queue_flight_resolution_audit', ['run_id'])
    op.create_index('ix_queue_flight_resolution_audit_airport_iata', 'queue_flight_resolution_audit', ['airport_iata'])
    op.create_index('ix_queue_flight_resolution_audit_flight_key', 'queue_flight_resolution_audit', ['flight_key'])

    op.create_table(
        'queue_virtual_wait_trace_audit',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('probe_time_utc', sa.DateTime(), nullable=False),
        sa.Column('probe_time_local', sa.DateTime(), nullable=True),
        sa.Column('server_count', sa.Integer(), nullable=False),
        sa.Column('busy_server_count', sa.Integer(), nullable=False),
        sa.Column('idle_server_count', sa.Integer(), nullable=False),
        sa.Column('backlog_count', sa.Float(), nullable=False),
        sa.Column('earliest_server_free_time_utc', sa.DateTime(), nullable=True),
        sa.Column('virtual_service_start_utc', sa.DateTime(), nullable=True),
        sa.Column('virtual_wait_minutes', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('run_id', 'airport_iata', 'process', 'probe_time_utc', name='uq_queue_virtual_wait_trace_audit'),
    )
    op.create_index('ix_queue_virtual_wait_trace_audit_run_id', 'queue_virtual_wait_trace_audit', ['run_id'])
    op.create_index('ix_queue_virtual_wait_trace_audit_airport_iata', 'queue_virtual_wait_trace_audit', ['airport_iata'])
    op.create_index('ix_queue_virtual_wait_trace_audit_probe_time_utc', 'queue_virtual_wait_trace_audit', ['probe_time_utc'])


def downgrade() -> None:
    op.drop_index('ix_queue_virtual_wait_trace_audit_probe_time_utc', table_name='queue_virtual_wait_trace_audit')
    op.drop_index('ix_queue_virtual_wait_trace_audit_airport_iata', table_name='queue_virtual_wait_trace_audit')
    op.drop_index('ix_queue_virtual_wait_trace_audit_run_id', table_name='queue_virtual_wait_trace_audit')
    op.drop_table('queue_virtual_wait_trace_audit')

    op.drop_index('ix_queue_flight_resolution_audit_flight_key', table_name='queue_flight_resolution_audit')
    op.drop_index('ix_queue_flight_resolution_audit_airport_iata', table_name='queue_flight_resolution_audit')
    op.drop_index('ix_queue_flight_resolution_audit_run_id', table_name='queue_flight_resolution_audit')
    op.drop_table('queue_flight_resolution_audit')

    op.drop_column('queue_dynamic_staffing_audit', 'winning_forecast')
    op.drop_column('queue_dynamic_staffing_audit', 'extended_needed_servers')
    op.drop_column('queue_dynamic_staffing_audit', 'normal_needed_servers')
    op.drop_column('queue_dynamic_staffing_audit', 'extended_lookahead_demand')
    op.drop_column('queue_dynamic_staffing_audit', 'normal_lookahead_demand')

    op.drop_column('queue_resource_config_audit', 'passport_arrival_dynamic_enabled')
    op.drop_column('queue_resource_config_audit', 'passport_departure_dynamic_enabled')
    op.drop_column('queue_resource_config_audit', 'security_intl_dynamic_enabled')
    op.drop_column('queue_resource_config_audit', 'allowed_levels_snapshot')

    op.drop_column('queue_flight_hour_contribution_audit', 'aircraft_capacity_source')
    op.drop_column('queue_flight_hour_contribution_audit', 'arr_time_source')
    op.drop_column('queue_flight_hour_contribution_audit', 'arr_time_effective_utc')
    op.drop_column('queue_flight_hour_contribution_audit', 'dep_time_source')
    op.drop_column('queue_flight_hour_contribution_audit', 'dep_time_effective_utc')
