"""queue calculation audit tables

Revision ID: 8c25b1ac4838
Revises: a4f2c9e17b3d
Create Date: 2026-09-27 12:00:00.000000

READ-ONLY audit trail for queue calculations, so results can be
inspected from phpMyAdmin/SQL directly (which flights contributed to
an hour, which profile percentage was used, how cohorts were split,
Schengen/passport routing, dynamic staffing checkpoints, and the
graph's own display aggregation). These tables are write-only from the
calculation's perspective - app/queue/audit.py copies already-computed
results into them; app/queue/engine.py and app/queue/domain/demand.py
never read from them, and nothing here changes queue math.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8c25b1ac4838'
down_revision: Union[str, Sequence[str], None] = 'a4f2c9e17b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'queue_calculation_hourly_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('airport_scale', sa.String(length=16), nullable=True),
        sa.Column('airport_timezone', sa.String(length=64), nullable=True),
        sa.Column('calculation_date', sa.Date(), nullable=True),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('window_start_utc', sa.DateTime(), nullable=False),
        sa.Column('window_start_local', sa.DateTime(), nullable=True),
        sa.Column('window_end_utc', sa.DateTime(), nullable=False),
        sa.Column('window_end_local', sa.DateTime(), nullable=True),
        sa.Column('direction', sa.String(length=16), nullable=True),
        sa.Column('flight_count', sa.Integer(), nullable=False),
        sa.Column('expected_passengers', sa.Float(), nullable=False),
        sa.Column('lane_or_desk_count', sa.Integer(), nullable=True),
        sa.Column('resource_type', sa.String(length=16), nullable=True),
        sa.Column('service_time_minutes', sa.Float(), nullable=True),
        sa.Column('capacity_per_resource_per_hour', sa.Float(), nullable=True),
        sa.Column('total_hourly_capacity', sa.Float(), nullable=True),
        sa.Column('backlog_start', sa.Float(), nullable=True),
        sa.Column('backlog_end', sa.Float(), nullable=True),
        sa.Column('estimated_wait_minutes', sa.Float(), nullable=True),
        sa.Column('risk', sa.String(length=16), nullable=True),
        sa.Column('display_status', sa.String(length=16), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'airport_iata', 'process', 'window_start_utc', name='uq_queue_calc_hourly_audit'),
    )
    op.create_index('ix_qcha_run_id', 'queue_calculation_hourly_audit', ['run_id'])
    op.create_index('ix_qcha_airport_date', 'queue_calculation_hourly_audit', ['airport_iata', 'calculation_date'])
    op.create_index('ix_qcha_airport_process_window', 'queue_calculation_hourly_audit', ['airport_iata', 'process', 'window_start_utc'])

    op.create_table(
        'queue_flight_hour_contribution_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('window_start_utc', sa.DateTime(), nullable=False),
        sa.Column('window_start_local', sa.DateTime(), nullable=True),
        sa.Column('window_end_local', sa.DateTime(), nullable=True),
        sa.Column('flight_db_id', sa.Integer(), nullable=True),
        sa.Column('flight_key', sa.String(length=64), nullable=False),
        sa.Column('flight_iata', sa.String(length=16), nullable=True),
        sa.Column('flight_icao', sa.String(length=16), nullable=True),
        sa.Column('airline_iata', sa.String(length=8), nullable=True),
        sa.Column('airline_icao', sa.String(length=8), nullable=True),
        sa.Column('direction', sa.String(length=16), nullable=False),
        sa.Column('dep_iata', sa.String(length=10), nullable=True),
        sa.Column('arr_iata', sa.String(length=10), nullable=True),
        sa.Column('dep_time_utc', sa.DateTime(), nullable=True),
        sa.Column('dep_time_local', sa.DateTime(), nullable=True),
        sa.Column('arr_time_utc', sa.DateTime(), nullable=True),
        sa.Column('arr_time_local', sa.DateTime(), nullable=True),
        sa.Column('is_domestic', sa.Boolean(), nullable=False),
        sa.Column('is_international', sa.Boolean(), nullable=False),
        sa.Column('is_schengen', sa.Boolean(), nullable=True),
        sa.Column('requires_passport', sa.Boolean(), nullable=True),
        sa.Column('aircraft_icao', sa.String(length=8), nullable=True),
        sa.Column('resolved_aircraft_capacity', sa.Integer(), nullable=True),
        sa.Column('profile_name', sa.String(length=32), nullable=False),
        sa.Column('profile_segment', sa.String(length=32), nullable=True),
        sa.Column('profile_percentage', sa.Float(), nullable=True),
        sa.Column('segment_start_utc', sa.DateTime(), nullable=True),
        sa.Column('segment_end_utc', sa.DateTime(), nullable=True),
        sa.Column('passengers_from_segment', sa.Float(), nullable=True),
        sa.Column('passengers_contributed_to_this_hour', sa.Float(), nullable=False),
        sa.Column('routing_source', sa.String(length=32), nullable=True),
        sa.Column('routing_destination', sa.String(length=32), nullable=True),
        sa.Column('security_arrival_source', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qfhca_run_id', 'queue_flight_hour_contribution_audit', ['run_id'])
    op.create_index('ix_qfhca_airport_process_window', 'queue_flight_hour_contribution_audit', ['airport_iata', 'process', 'window_start_utc'])
    op.create_index('ix_qfhca_flight_key', 'queue_flight_hour_contribution_audit', ['flight_key'])

    op.create_table(
        'queue_cohort_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('flight_key', sa.String(length=64), nullable=False),
        sa.Column('flight_iata', sa.String(length=16), nullable=True),
        sa.Column('direction', sa.String(length=16), nullable=False),
        sa.Column('profile_segment', sa.String(length=32), nullable=True),
        sa.Column('profile_percentage', sa.Float(), nullable=True),
        sa.Column('aircraft_capacity', sa.Integer(), nullable=True),
        sa.Column('cohort_start_utc', sa.DateTime(), nullable=False),
        sa.Column('cohort_start_local', sa.DateTime(), nullable=True),
        sa.Column('cohort_resolution_minutes', sa.Integer(), nullable=False),
        sa.Column('passenger_count', sa.Float(), nullable=False),
        sa.Column('routing_stage', sa.String(length=32), nullable=True),
        sa.Column('source_process', sa.String(length=32), nullable=True),
        sa.Column('destination_process', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qca_run_id', 'queue_cohort_audit', ['run_id'])
    op.create_index('ix_qca_flight_key', 'queue_cohort_audit', ['flight_key'])
    op.create_index('ix_qca_process_cohort_start', 'queue_cohort_audit', ['process', 'cohort_start_utc'])

    op.create_table(
        'queue_service_event_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('source_flight_keys', sa.Text(), nullable=True),
        sa.Column('arrival_time_utc', sa.DateTime(), nullable=False),
        sa.Column('arrival_time_local', sa.DateTime(), nullable=True),
        sa.Column('service_start_time_utc', sa.DateTime(), nullable=False),
        sa.Column('service_start_time_local', sa.DateTime(), nullable=True),
        sa.Column('completion_time_utc', sa.DateTime(), nullable=False),
        sa.Column('completion_time_local', sa.DateTime(), nullable=True),
        sa.Column('wait_minutes', sa.Float(), nullable=False),
        sa.Column('passenger_count', sa.Float(), nullable=False),
        sa.Column('resource_count', sa.Integer(), nullable=True),
        sa.Column('service_time_minutes', sa.Float(), nullable=True),
        sa.Column('backlog_before', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qsea_run_id', 'queue_service_event_audit', ['run_id'])
    op.create_index('ix_qsea_airport_process_arrival', 'queue_service_event_audit', ['airport_iata', 'process', 'arrival_time_utc'])

    op.create_table(
        'queue_routing_summary_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('calculation_date', sa.Date(), nullable=True),
        sa.Column('total_physical_flights', sa.Integer(), nullable=False),
        sa.Column('domestic_departure_flights', sa.Integer(), nullable=False),
        sa.Column('international_departure_flights', sa.Integer(), nullable=False),
        sa.Column('domestic_arrival_flights', sa.Integer(), nullable=False),
        sa.Column('international_arrival_flights', sa.Integer(), nullable=False),
        sa.Column('schengen_departure_flights', sa.Integer(), nullable=False),
        sa.Column('non_schengen_departure_flights', sa.Integer(), nullable=False),
        sa.Column('schengen_arrival_flights', sa.Integer(), nullable=False),
        sa.Column('non_schengen_arrival_flights', sa.Integer(), nullable=False),
        sa.Column('schengen_departures_bypassed_passport', sa.Integer(), nullable=False),
        sa.Column('non_schengen_departures_entered_passport', sa.Integer(), nullable=False),
        sa.Column('schengen_arrivals_bypassed_passport', sa.Integer(), nullable=False),
        sa.Column('non_schengen_arrivals_entered_passport', sa.Integer(), nullable=False),
        sa.Column('codeshare_records_removed', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'airport_iata', name='uq_queue_routing_summary_audit'),
    )
    op.create_index('ix_qrsa_run_id', 'queue_routing_summary_audit', ['run_id'])
    op.create_index('ix_qrsa_airport_date', 'queue_routing_summary_audit', ['airport_iata', 'calculation_date'])

    op.create_table(
        'queue_country_routing_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('calculation_date', sa.Date(), nullable=True),
        sa.Column('country_code', sa.String(length=4), nullable=True),
        sa.Column('country_name', sa.String(length=100), nullable=True),
        sa.Column('direction', sa.String(length=16), nullable=False),
        sa.Column('flight_type', sa.String(length=16), nullable=False),
        sa.Column('schengen_status', sa.String(length=16), nullable=False),
        sa.Column('flight_count', sa.Integer(), nullable=False),
        sa.Column('passenger_count', sa.Float(), nullable=False),
        sa.Column('passport_required_count', sa.Integer(), nullable=False),
        sa.Column('passport_bypass_count', sa.Integer(), nullable=False),
        sa.Column('security_dom_passengers', sa.Float(), nullable=False),
        sa.Column('passport_dep_passengers', sa.Float(), nullable=False),
        sa.Column('security_intl_passengers', sa.Float(), nullable=False),
        sa.Column('passport_arr_passengers', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qcra_run_id', 'queue_country_routing_audit', ['run_id'])
    op.create_index('ix_qcra_country_code', 'queue_country_routing_audit', ['country_code'])
    op.create_index('ix_qcra_airport_date', 'queue_country_routing_audit', ['airport_iata', 'calculation_date'])

    op.create_table(
        'queue_resource_config_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('airport_scale', sa.String(length=16), nullable=True),
        sa.Column('domestic_security_lanes', sa.Integer(), nullable=True),
        sa.Column('international_security_lanes', sa.Integer(), nullable=True),
        sa.Column('departure_passport_desks_base', sa.Integer(), nullable=True),
        sa.Column('departure_passport_desks_max', sa.Integer(), nullable=True),
        sa.Column('arrival_passport_desks_base', sa.Integer(), nullable=True),
        sa.Column('arrival_passport_desks_max', sa.Integer(), nullable=True),
        sa.Column('security_service_time_minutes', sa.Float(), nullable=True),
        sa.Column('passport_service_time_minutes', sa.Float(), nullable=True),
        sa.Column('domestic_security_capacity_per_hour', sa.Float(), nullable=True),
        sa.Column('international_security_capacity_per_hour', sa.Float(), nullable=True),
        sa.Column('config_source', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qrca_run_id', 'queue_resource_config_audit', ['run_id'])
    op.create_index('ix_qrca_airport', 'queue_resource_config_audit', ['airport_iata'])

    op.create_table(
        'queue_dynamic_staffing_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('checkpoint_time_utc', sa.DateTime(), nullable=False),
        sa.Column('checkpoint_time_local', sa.DateTime(), nullable=True),
        sa.Column('previous_server_count', sa.Integer(), nullable=True),
        sa.Column('new_server_count', sa.Integer(), nullable=False),
        sa.Column('backlog', sa.Float(), nullable=True),
        sa.Column('lookahead_demand', sa.Float(), nullable=True),
        sa.Column('needed_servers', sa.Integer(), nullable=True),
        sa.Column('ramp_delta', sa.Integer(), nullable=True),
        sa.Column('pending_retirements', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qdsa_run_id', 'queue_dynamic_staffing_audit', ['run_id'])
    op.create_index('ix_qdsa_airport_process_checkpoint', 'queue_dynamic_staffing_audit', ['airport_iata', 'process', 'checkpoint_time_utc'])

    op.create_table(
        'queue_graph_display_audit',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('airport_iata', sa.String(length=10), nullable=False),
        sa.Column('process', sa.String(length=16), nullable=False),
        sa.Column('bucket_start_utc', sa.DateTime(), nullable=False),
        sa.Column('bucket_start_local', sa.DateTime(), nullable=True),
        sa.Column('source_resolution_minutes', sa.Integer(), nullable=False),
        sa.Column('visual_bucket_minutes', sa.Integer(), nullable=False),
        sa.Column('source_point_count', sa.Integer(), nullable=False),
        sa.Column('average_wait_minutes', sa.Float(), nullable=True),
        sa.Column('peak_wait_minutes', sa.Float(), nullable=True),
        sa.Column('display_wait_minutes', sa.Float(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=True),
        sa.Column('is_current', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_qgda_run_id', 'queue_graph_display_audit', ['run_id'])
    op.create_index('ix_qgda_airport_process_bucket', 'queue_graph_display_audit', ['airport_iata', 'process', 'bucket_start_utc'])


def downgrade() -> None:
    op.drop_table('queue_graph_display_audit')
    op.drop_table('queue_dynamic_staffing_audit')
    op.drop_table('queue_resource_config_audit')
    op.drop_table('queue_country_routing_audit')
    op.drop_table('queue_routing_summary_audit')
    op.drop_table('queue_service_event_audit')
    op.drop_table('queue_cohort_audit')
    op.drop_table('queue_flight_hour_contribution_audit')
    op.drop_table('queue_calculation_hourly_audit')
