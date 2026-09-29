"""passenger-weighted display wait (remove virtual arrival wait)

Revision ID: b4d8f1c7e3a2
Revises: a7c3e5f9d2b1
Create Date: 2026-09-29 13:00:00.000000

ADIM (Passenger-Weighted Display Wait) - kullanıcı talebi: 5dk/30dk
display/graph metriği artık "queue-state/virtual-arrival wait" (o an
bir yolcu gelse ne kadar beklerdi, `virtual_arrival_wait`) DEĞİL,
"o pencerede arrival_time'ı düşen GERÇEK service event'lerin passenger-
weighted ortalama wait'i" (`SUM(wait*pax)/SUM(pax)`).

Dependency audit sonucu `virtual_arrival_wait()`/`virtual_arrival_wait_
detailed()` SADECE display/audit için kullanılıyordu - dynamic staffing
(`simulate_fifo_queue_dynamic`) kendi backlog/lookahead hesabını queue
deque'inden DOĞRUDAN yapıyor, hourly `QueuePrediction` da ayrı/zaten
passenger-weighted (`_bucket_weighted_wait`) bir hesap kullanıyor -
hiçbiri virtual wait'e bağımlı DEĞİLDİ. Bu yüzden production-critical
DEĞİL, TAMAMEN kaldırıldı (kod + bu migration'daki tablo).

Bu migration:
1. `queue_virtual_wait_trace_audit` tablosunu DROP eder (artık hiçbir
   writer/consumer yok - salt bu görev için, önceki bir görevde
   eklenmişti, şimdi kaldırılan mekanizmanın audit kopyasıydı).
2. `queue_wait_display_5m`'e `wait_numerator`/`passenger_count`/
   `aggregation_method` ekler - "bu 5dk değer neden X?" sorusunu SQL'den
   join'siz cevaplamak için (`estimated_wait_minutes` kolonu adı/anlamı
   GERİYE DÖNÜK UYUMLULUK için korundu, artık weighted-average taşıyor).
3. `queue_graph_display_audit`'e `wait_numerator`/`passenger_count`
   ekler - 30dk bar'ın ARTIK "6x5dk basit ortalama" değil, "30dk'daki
   TÜM yolcular üzerinden SUM(wait*pax)/SUM(pax)" olduğunu kanıtlamak
   için.

Additive/DROP - mevcut queue FIFO/dynamic staffing/hourly prediction
matematiği bu migration'ın KENDİSİYLE değişmiyor.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d8f1c7e3a2'
down_revision: Union[str, Sequence[str], None] = 'a7c3e5f9d2b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('queue_wait_display_5m', sa.Column('wait_numerator', sa.Float(), nullable=True))
    op.add_column('queue_wait_display_5m', sa.Column('passenger_count', sa.Float(), nullable=True))
    op.add_column('queue_wait_display_5m', sa.Column('aggregation_method', sa.String(length=40), nullable=True))

    op.add_column('queue_graph_display_audit', sa.Column('wait_numerator', sa.Float(), nullable=True))
    op.add_column('queue_graph_display_audit', sa.Column('passenger_count', sa.Float(), nullable=True))

    op.drop_table('queue_virtual_wait_trace_audit')


def downgrade() -> None:
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

    op.drop_column('queue_graph_display_audit', 'passenger_count')
    op.drop_column('queue_graph_display_audit', 'wait_numerator')

    op.drop_column('queue_wait_display_5m', 'aggregation_method')
    op.drop_column('queue_wait_display_5m', 'passenger_count')
    op.drop_column('queue_wait_display_5m', 'wait_numerator')
