"""turkish audit display columns

Revision ID: d1e5f6a2b8c4
Revises: 8c25b1ac4838
Create Date: 2026-09-28 01:30:00.000000

Renames PHYSICAL SQL column names (only) on the 9 write-only, read-only
audit tables (bkz. app/queue/audit.py) so they read naturally in
phpMyAdmin/SQL for a Turkish-speaking operator. This is a display-only
change:

  - Python/ORM attribute names on the model classes in app/queue/models.py
    are UNCHANGED (e.g. `row.expected_passengers` keeps working exactly
    as before) - only the underlying `mapped_column("...")` physical name
    argument changed, via SQLAlchemy's column-name-vs-attribute-name split.
  - No production/runtime code, no API, and no test asserts on the
    PHYSICAL column name string - only via the ORM attribute - so nothing
    outside this migration + models.py needed to change (verified via
    repo-wide search before writing this migration).
  - Columns that ARE read by production/runtime logic conceptually
    (run_id, airport_iata, flight_iata/icao, dep/arr iata, all *_utc/
    *_local timestamps, process, direction, source_flight_keys,
    created_at, country_code, etc.) are left in English on purpose - they
    stay English in both Python and SQL.
  - `queue_routing_summary_audit` and `queue_resource_config_audit` are
    NOT touched - none of their columns were on the explicit rename list.
  - Uses ALTER TABLE ... RENAME COLUMN (MySQL 8.0+): this is a pure
    metadata rename - existing rows, indexes, and unique constraints are
    preserved automatically. No data is dropped or recreated.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1e5f6a2b8c4'
down_revision: Union[str, Sequence[str], None] = '8c25b1ac4838'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, old_physical_name, new_physical_name, existing_type, nullable)
_RENAMES = [
    ("queue_calculation_hourly_audit", "flight_count", "ucus_sayisi", sa.Integer(), False),
    ("queue_calculation_hourly_audit", "expected_passengers", "beklenen_yolcu_sayisi", sa.Float(), False),
    ("queue_calculation_hourly_audit", "lane_or_desk_count", "lane_veya_gise_sayisi", sa.Integer(), True),
    ("queue_calculation_hourly_audit", "resource_type", "kaynak_tipi", sa.String(length=16), True),
    ("queue_calculation_hourly_audit", "service_time_minutes", "islem_suresi_dakika", sa.Float(), True),
    ("queue_calculation_hourly_audit", "capacity_per_resource_per_hour", "kaynak_basina_saatlik_kapasite", sa.Float(), True),
    ("queue_calculation_hourly_audit", "total_hourly_capacity", "toplam_saatlik_kapasite", sa.Float(), True),
    ("queue_calculation_hourly_audit", "backlog_start", "saat_basi_bekleyen_yolcu", sa.Float(), True),
    ("queue_calculation_hourly_audit", "backlog_end", "saat_sonu_bekleyen_yolcu", sa.Float(), True),
    ("queue_calculation_hourly_audit", "estimated_wait_minutes", "tahmini_bekleme_dakika", sa.Float(), True),
    ("queue_calculation_hourly_audit", "display_status", "gorunum_durumu", sa.String(length=16), True),

    ("queue_flight_hour_contribution_audit", "resolved_aircraft_capacity", "cozumlenen_ucak_kapasitesi", sa.Integer(), True),
    ("queue_flight_hour_contribution_audit", "profile_name", "profil_adi", sa.String(length=32), False),
    ("queue_flight_hour_contribution_audit", "profile_segment", "profil_segmenti", sa.String(length=32), True),
    ("queue_flight_hour_contribution_audit", "profile_percentage", "profil_yuzdesi", sa.Float(), True),
    ("queue_flight_hour_contribution_audit", "passengers_from_segment", "segmentten_gelen_yolcu", sa.Float(), True),
    ("queue_flight_hour_contribution_audit", "passengers_contributed_to_this_hour", "bu_saate_katilan_yolcu", sa.Float(), False),

    ("queue_cohort_audit", "profile_segment", "profil_segmenti", sa.String(length=32), True),
    ("queue_cohort_audit", "profile_percentage", "profil_yuzdesi", sa.Float(), True),
    ("queue_cohort_audit", "aircraft_capacity", "ucak_kapasitesi", sa.Integer(), True),
    ("queue_cohort_audit", "cohort_resolution_minutes", "cohort_cozunurluk_dakika", sa.Integer(), False),
    ("queue_cohort_audit", "passenger_count", "yolcu_sayisi", sa.Float(), False),

    ("queue_service_event_audit", "wait_minutes", "bekleme_dakika", sa.Float(), False),
    ("queue_service_event_audit", "passenger_count", "yolcu_sayisi", sa.Float(), False),
    ("queue_service_event_audit", "resource_count", "kaynak_sayisi", sa.Integer(), True),
    ("queue_service_event_audit", "service_time_minutes", "islem_suresi_dakika", sa.Float(), True),
    ("queue_service_event_audit", "backlog_before", "islem_oncesi_backlog", sa.Float(), True),

    ("queue_country_routing_audit", "country_name", "ulke_adi", sa.String(length=100), True),
    ("queue_country_routing_audit", "flight_type", "ucus_tipi", sa.String(length=16), False),
    ("queue_country_routing_audit", "schengen_status", "schengen_durumu", sa.String(length=16), False),
    ("queue_country_routing_audit", "flight_count", "ucus_sayisi", sa.Integer(), False),
    ("queue_country_routing_audit", "passenger_count", "yolcu_sayisi", sa.Float(), False),
    ("queue_country_routing_audit", "passport_required_count", "pasaport_gereken_yolcu_sayisi", sa.Integer(), False),
    ("queue_country_routing_audit", "passport_bypass_count", "pasaport_atlayan_yolcu_sayisi", sa.Integer(), False),
    ("queue_country_routing_audit", "security_dom_passengers", "domestic_security_yolcu_sayisi", sa.Float(), False),
    ("queue_country_routing_audit", "passport_dep_passengers", "departure_passport_yolcu_sayisi", sa.Float(), False),
    ("queue_country_routing_audit", "security_intl_passengers", "international_security_yolcu_sayisi", sa.Float(), False),
    ("queue_country_routing_audit", "passport_arr_passengers", "arrival_passport_yolcu_sayisi", sa.Float(), False),

    ("queue_dynamic_staffing_audit", "previous_server_count", "onceki_gise_sayisi", sa.Integer(), True),
    ("queue_dynamic_staffing_audit", "new_server_count", "yeni_gise_sayisi", sa.Integer(), False),
    ("queue_dynamic_staffing_audit", "lookahead_demand", "ileri_bakis_talep", sa.Float(), True),
    ("queue_dynamic_staffing_audit", "needed_servers", "gereken_gise_sayisi", sa.Integer(), True),
    ("queue_dynamic_staffing_audit", "ramp_delta", "gise_degisimi", sa.Integer(), True),
    ("queue_dynamic_staffing_audit", "pending_retirements", "kapanmayi_bekleyen_gise_sayisi", sa.Integer(), True),

    ("queue_graph_display_audit", "source_point_count", "kaynak_nokta_sayisi", sa.Integer(), False),
    ("queue_graph_display_audit", "average_wait_minutes", "ortalama_bekleme_dakika", sa.Float(), True),
    ("queue_graph_display_audit", "peak_wait_minutes", "maksimum_bekleme_dakika", sa.Float(), True),
    ("queue_graph_display_audit", "display_wait_minutes", "grafikte_gosterilen_bekleme_dakika", sa.Float(), True),
]


def upgrade() -> None:
    for table, old_name, new_name, existing_type, nullable in _RENAMES:
        op.alter_column(
            table, old_name,
            new_column_name=new_name,
            existing_type=existing_type,
            existing_nullable=nullable,
        )


def downgrade() -> None:
    for table, old_name, new_name, existing_type, nullable in _RENAMES:
        op.alter_column(
            table, new_name,
            new_column_name=old_name,
            existing_type=existing_type,
            existing_nullable=nullable,
        )
