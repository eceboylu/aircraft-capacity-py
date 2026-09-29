"""
READ-ONLY calculation audit trail.

Guarantee (repo-wide, enforced by tests/test_audit_isolation.py):
`app/queue/engine.py` and `app/queue/domain/*.py` (the actual queue
calculation) NEVER import this module and NEVER read any table defined
here. This module only ever runs AFTER a calculation has already
finished, and only ever COPIES already-computed results (or re-derives
them via the SAME pure functions demand.py already exposes) into
audit tables, for inspection via phpMyAdmin/SQL. Disabling audit
(QUEUE_CALCULATION_AUDIT_ENABLED=false) must not change any queue
calculation result - it only stops these inserts.

Flow: existing production calculation -> result -> audit rows persisted.
Never: audit tables -> calculation.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import date as date_type, datetime, timedelta, timezone

from .constants import (
    ARRIVAL_RELEASE_BUCKET_MINUTES,
    DEPARTURE_SHOWUP_BUCKET_MINUTES,
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
    LOCATION_DOMESTIC,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from .core.scoring import (
    domestic_security_capacity_rate,
    international_security_capacity_rate,
)
from .domain.demand import (
    _arrival_release_base,
    _departure_show_up_base,
    arrival_passenger_release_events_detailed,
    departure_show_up_events_detailed,
)
from .domain.flows import (
    is_domestic_departure,
    is_international_arrival,
    is_international_departure,
    is_international_arrival_requiring_passport,
    is_international_departure_requiring_passport,
    is_schengen_departure_skipping_passport,
)
from .domain.schengen import is_schengen_country
from .models import (
    QueueAirportDepartureFlow5m,
    QueueCalculationHourlyAudit,
    QueueCohortAudit,
    QueueCountryRoutingAudit,
    QueueDynamicStaffingAudit,
    QueueFlightHourContributionAudit,
    QueueFlightResolutionAudit,
    QueueGraphDisplayAudit,
    QueueResourceConfigAudit,
    QueueRoutingSummaryAudit,
    QueueServiceEventAudit,
)

logger = logging.getLogger(__name__)

AUDIT_INSERT_CHUNK_SIZE = 500

# Sadece GÖRÜNTÜLEME kolaylığı - hesaplamanın hiçbir yerinde KULLANILMAZ
# (routing/Schengen kararı zaten `country_code`'un kendisinden ve
# `domain/schengen.py:SCHENGEN_COUNTRY_CODES`'tan geliyor). Kapsamayan
# bir kod için sadece `country_name=None` kalır, kod (`country_code`)
# yine de doğru şekilde gösterilir.
COUNTRY_NAMES = {
    "TR": "Türkiye", "DE": "Germany", "CH": "Switzerland", "FR": "France",
    "GB": "United Kingdom", "NL": "Netherlands", "IT": "Italy", "ES": "Spain",
    "AT": "Austria", "BE": "Belgium", "GR": "Greece", "PT": "Portugal",
    "PL": "Poland", "SE": "Sweden", "NO": "Norway", "DK": "Denmark",
    "FI": "Finland", "IE": "Ireland", "CZ": "Czechia", "HU": "Hungary",
    "RO": "Romania", "BG": "Bulgaria", "HR": "Croatia", "SI": "Slovenia",
    "SK": "Slovakia", "LU": "Luxembourg", "MT": "Malta", "CY": "Cyprus",
    "IS": "Iceland", "LI": "Liechtenstein", "US": "United States",
    "AE": "United Arab Emirates", "QA": "Qatar",
    "RU": "Russia", "AZ": "Azerbaijan", "GE": "Georgia", "IL": "Israel",
    "EG": "Egypt", "MA": "Morocco", "TN": "Tunisia", "DZ": "Algeria",
}


def audit_enabled() -> bool:
    return os.environ.get("QUEUE_CALCULATION_AUDIT_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def audit_retention_days() -> int:
    raw = os.environ.get("QUEUE_AUDIT_RETENTION_DAYS", "7")
    try:
        return max(0, int(raw))
    except ValueError:
        return 7


def new_run_id() -> str:
    return str(uuid.uuid4())


def _to_local(moment_utc: datetime | None, tz) -> datetime | None:
    if moment_utc is None or tz is None:
        return None
    aware = moment_utc.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz).replace(tzinfo=None)


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _flight_shares_of(source_flight_keys) -> dict[str, float]:
    return source_flight_keys or {}


def _time_source_label(actual, estimated, scheduled) -> str | None:
    if actual is not None:
        return "actual"
    if estimated is not None:
        return "estimated"
    if scheduled is not None:
        return "scheduled"
    return None


def _is_domestic_arrival(flight) -> bool:
    return flight.direction == DIRECTION_ARRIVAL and flight.location == LOCATION_DOMESTIC


def _is_schengen_arrival_bypassing_passport(flight) -> bool:
    return is_international_arrival(flight) and not getattr(flight, "requires_passport", True)


def _bulk_add(session, rows: list) -> None:
    for start in range(0, len(rows), AUDIT_INSERT_CHUNK_SIZE):
        session.add_all(rows[start:start + AUDIT_INSERT_CHUNK_SIZE])
        session.flush()


# ---------------------------------------------------------------------------
# Table 5 (resource config snapshot)
# ---------------------------------------------------------------------------

def _allowed_levels_snapshot(config) -> str | None:
    """`_dynamic_staffing_params_for()` (engine.py) SADECE lazy import
    edilir - audit.py'nin bu 3 sürecin `allowed_levels`'ını GÖRÜNTÜLEMEK
    için AYNI, tek kaynak formülü (`_operational_levels_for`) tekrar
    hesaplamak yerine ÇAĞIRMASI için (Bölüm 18 - "farklı formülle üretme"
    kuralı)."""
    from .engine import _dynamic_staffing_params_for

    parts = []
    for label, pool in (
        ("security_intl", "security_intl"),
        ("passport_dep", "departure"),
        ("passport_arr", "arrival"),
    ):
        params = _dynamic_staffing_params_for(config, pool)
        if params is not None and params.allowed_levels:
            parts.append(f"{label}={','.join(str(lvl) for lvl in params.allowed_levels)}")
    return ";".join(parts) if parts else None


def record_resource_config_audit(session, run_id: str, airport_iata: str, config) -> None:
    row = QueueResourceConfigAudit(
        run_id=run_id,
        airport_iata=airport_iata,
        airport_scale=config.scale,
        domestic_security_lanes=config.domestic_security_lane_count,
        international_security_lanes=config.international_security_lane_count,
        international_security_lanes_max=config.international_security_lane_count_max,
        departure_passport_desks_base=config.passport_departure_server_count,
        departure_passport_desks_max=config.passport_departure_server_count_max,
        arrival_passport_desks_base=config.passport_arrival_server_count,
        arrival_passport_desks_max=config.passport_arrival_server_count_max,
        security_control_interval_minutes=config.security_dynamic_control_interval_minutes,
        passport_control_interval_minutes=config.passport_dynamic_control_interval_minutes,
        passport_departure_control_interval_minutes=config.passport_departure_control_interval_minutes,
        passport_arrival_control_interval_minutes=config.passport_arrival_control_interval_minutes,
        dynamic_target_utilization=config.dynamic_target_utilization,
        passport_arrival_lookahead_minutes=config.passport_arrival_lookahead_minutes,
        security_service_time_minutes=config.security_service_time_minutes,
        passport_service_time_minutes=config.passport_service_time_minutes,
        domestic_security_capacity_per_hour=domestic_security_capacity_rate(config) * 60,
        international_security_capacity_per_hour=international_security_capacity_rate(config) * 60,
        config_source="scale_default" if config.is_default else "airport_specific_db_override",
        allowed_levels_snapshot=_allowed_levels_snapshot(config),
        security_intl_dynamic_enabled=config.security_intl_dynamic,
        passport_departure_dynamic_enabled=config.passport_departure_dynamic,
        passport_arrival_dynamic_enabled=config.passport_arrival_dynamic,
    )
    session.add(row)
    session.flush()


# ---------------------------------------------------------------------------
# Tables 2/3 (per-flight hourly contribution + per-cohort detail)
# ---------------------------------------------------------------------------

def _classify_flight(flight, country_by_iata: dict[str, str]) -> dict:
    dep_country = country_by_iata.get((flight.dep_iata or "").upper())
    arr_country = country_by_iata.get((flight.arr_iata or "").upper())
    is_domestic = is_domestic_departure(flight) or _is_domestic_arrival(flight)
    is_international = is_international_departure(flight) or is_international_arrival(flight)

    is_schengen = (
        is_schengen_country(dep_country) and is_schengen_country(arr_country)
        if is_international else None
    )

    return {
        "dep_country": dep_country,
        "arr_country": arr_country,
        "is_domestic": is_domestic,
        "is_international": is_international,
        "is_schengen": is_schengen,
    }


def record_flight_cohort_and_contribution_audit(
    session, run_id: str, airport_iata: str, flights: list, demand, tz,
    country_by_iata: dict[str, str], coupling: dict,
) -> None:
    """
    Her flight için: (a) `queue_cohort_audit`'e HER 5dk/1dk cohort satırı,
    (b) `queue_flight_hour_contribution_audit`'e her (flight, profile
    segment, saat) kombinasyonu için bir özet satır ekler.

    Cohort sayıları `departure_show_up_events_detailed()`/`arrival_
    passenger_release_events_detailed()` (demand.py, PRODUCTION'ın
    KENDİ kullandığı fonksiyonların detaylı/read-only varyantı) ile
    üretilir - farklı bir hesap İCAT EDİLMEZ. Non-Schengen departure'ların
    security_intl'e GERÇEKTEN hangi saatte ulaştığı, zaten hesaplanmış
    `coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]` ServiceEvent
    listesindeki `source_flight_keys`'ten okunur (yeniden simüle EDİLMEZ).
    """
    cohort_rows: list[QueueCohortAudit] = []
    contribution_rows: list[QueueFlightHourContributionAudit] = []
    # ADIM (Airport Departure Passenger Flow) - queue wait DEĞİL, "kaç
    # departure yolcusu havalimanına GELİYOR" sorusu için, AYNI `detailed`
    # (cohort_time, count) listesinden TEK bir ek geçişte, sınıflandırma
    # bazında (domestic/schengen/non_schengen) toplanan bir yan-ürün
    # akümülatör - bkz. `_accumulate_departure_flow`/`record_airport_
    # departure_flow_5m` altta.
    departure_flow_by_bucket: dict[datetime, dict] = defaultdict(
        lambda: {"domestic": 0.0, "schengen": 0.0, "non_schengen": 0.0, "flight_keys": set()}
    )

    passport_dep_events = coupling.get("process_events", {}).get(PROCESS_PASSPORT_DEPARTURE, [])

    def _real_completions_for_flight(events, flight_key: str) -> dict[datetime, float]:
        """Bu flight'a ait GERÇEK passport completion pax sayısını saate
        göre toplar - `event.count` (event'teki TÜM flight'ların toplamı)
        DEĞİL, `event.source_flight_keys[flight_key]` (bu flight'ın PAYI,
        birden fazla flight aynı completion event'inde birleşse bile
        ÇİFTE SAYIM yapmaz - bkz. `_as_key_shares`/`_merge_adjacent_
        events`)."""
        by_hour: dict[datetime, float] = {}
        for event in events:
            share = _flight_shares_of(event.source_flight_keys).get(flight_key)
            if not share:
                continue
            hour = _floor_hour(event.completion_time)
            by_hour[hour] = by_hour.get(hour, 0.0) + share
        return by_hour

    for flight in flights:
        if flight.status in EXCLUDED_STATUSES:
            continue
        classification = _classify_flight(flight, country_by_iata)
        total_demand = demand.passenger_demand(flight)
        if total_demand <= 0:
            continue

        common = dict(
            run_id=run_id, airport_iata=airport_iata,
            flight_db_id=getattr(flight, "id", None),
            flight_key=flight.flight_key,
            flight_iata=getattr(flight, "flight_iata", None),
            flight_icao=None,
            airline_iata=getattr(flight, "airline_iata", None),
            airline_icao=None,
            direction=flight.direction,
            dep_iata=flight.dep_iata, arr_iata=flight.arr_iata,
            dep_time_utc=flight.dep_scheduled_utc, dep_time_local=_to_local(flight.dep_scheduled_utc, tz),
            arr_time_utc=flight.arr_scheduled_utc, arr_time_local=_to_local(flight.arr_scheduled_utc, tz),
            is_domestic=classification["is_domestic"], is_international=classification["is_international"],
            is_schengen=classification["is_schengen"],
            requires_passport=getattr(flight, "requires_passport", True),
            aircraft_icao=getattr(flight, "aircraft_icao", None),
            resolved_aircraft_capacity=total_demand,
            dep_time_effective_utc=_departure_show_up_base(flight),
            dep_time_source=_time_source_label(
                flight.dep_actual_utc, flight.dep_estimated_utc, flight.dep_scheduled_utc,
            ),
            arr_time_effective_utc=_arrival_release_base(flight),
            arr_time_source=_time_source_label(
                flight.arr_actual_utc, flight.arr_estimated_utc, flight.arr_scheduled_utc,
            ),
            aircraft_capacity_source=demand.capacity_result(flight).source,
        )

        # ---- DOMESTIC DEPARTURE -> security_dom (direct show-up) ----
        if is_domestic_departure(flight):
            detailed = departure_show_up_events_detailed(flight, total_demand)
            _emit_departure_cohorts_and_contributions(
                cohort_rows, contribution_rows, common, detailed,
                process=PROCESS_SECURITY_DOMESTIC, tz=tz,
                routing_source="departure_show_up", routing_destination=PROCESS_SECURITY_DOMESTIC,
                security_arrival_source="direct_show_up",
            )
            _accumulate_departure_flow(departure_flow_by_bucket, detailed, "domestic", flight.flight_key)

        # ---- SCHENGEN INTERNATIONAL DEPARTURE -> security_intl direct ----
        elif is_schengen_departure_skipping_passport(flight):
            detailed = departure_show_up_events_detailed(flight, total_demand)
            _emit_departure_cohorts_and_contributions(
                cohort_rows, contribution_rows, common, detailed,
                process=PROCESS_SECURITY_INTL, tz=tz,
                routing_source="departure_show_up", routing_destination=PROCESS_SECURITY_INTL,
                security_arrival_source="direct_show_up",
            )
            _accumulate_departure_flow(departure_flow_by_bucket, detailed, "schengen", flight.flight_key)

        # ---- NON-SCHENGEN INTERNATIONAL DEPARTURE -> passport_dep -> security_intl ----
        elif is_international_departure_requiring_passport(flight):
            detailed = departure_show_up_events_detailed(flight, total_demand)
            _emit_departure_cohorts_and_contributions(
                cohort_rows, contribution_rows, common, detailed,
                process=PROCESS_PASSPORT_DEPARTURE, tz=tz,
                routing_source="departure_show_up", routing_destination=PROCESS_PASSPORT_DEPARTURE,
                security_arrival_source="passport_completion",
            )
            _accumulate_departure_flow(departure_flow_by_bucket, detailed, "non_schengen", flight.flight_key)
            real_hours = _real_completions_for_flight(passport_dep_events, flight.flight_key)
            for hour, passengers in sorted(real_hours.items()):
                contribution_rows.append(QueueFlightHourContributionAudit(
                    **common,
                    process=PROCESS_SECURITY_INTL,
                    window_start_utc=hour, window_start_local=_to_local(hour, tz),
                    window_end_local=_to_local(hour + timedelta(hours=1), tz),
                    profile_name="departure_show_up",
                    profile_segment=None, profile_percentage=None,
                    segment_start_utc=None, segment_end_utc=None,
                    passengers_from_segment=None,
                    passengers_contributed_to_this_hour=passengers,
                    routing_source="passport_completion",
                    routing_destination=PROCESS_SECURITY_INTL,
                    security_arrival_source="passport_completion",
                ))

        # ---- NON-SCHENGEN INTERNATIONAL ARRIVAL -> passport_arr -> exit ----
        if is_international_arrival_requiring_passport(flight):
            detailed = arrival_passenger_release_events_detailed(flight, total_demand)
            _emit_arrival_cohorts_and_contributions(
                cohort_rows, contribution_rows, common, detailed,
                process=PROCESS_PASSPORT_ARRIVAL, tz=tz,
                routing_source="arrival_release", routing_destination=PROCESS_PASSPORT_ARRIVAL,
                security_arrival_source=None,
            )
        # Schengen arrival / domestic arrival: kasıtlı olarak HİÇBİR
        # cohort/contribution satırı üretilmiyor - modeled queue YOK
        # (bkz. section 15/19/22, requires_passport=False veya domestic
        # arrival zaten common'da görünüyor, ama process satırı yok).

    _bulk_add(session, cohort_rows)
    _bulk_add(session, contribution_rows)
    record_airport_departure_flow_5m(session, run_id, airport_iata, departure_flow_by_bucket, tz)


def _accumulate_departure_flow(
    bucket_map: dict[datetime, dict], detailed, category: str, flight_key: str,
) -> None:
    """`detailed`: `departure_show_up_events_detailed()`'in ZATEN ürettiği
    `(cohort_time, count, segment)` üçlüleri - burada YENİDEN üretilmiyor,
    SADECE `category` (domestic/schengen/non_schengen) bazında 5dk
    bucket'a göre toplanıyor (Airport Departure Passenger Flow, queue
    wait DEĞİL)."""
    for cohort_time, count, _segment in detailed:
        bucket = bucket_map[cohort_time]
        bucket[category] += count
        bucket["flight_keys"].add(flight_key)


def record_airport_departure_flow_5m(
    session, run_id: str, airport_iata: str, bucket_map: dict[datetime, dict], tz,
) -> None:
    """`queue_airport_departure_flow_5m`'e yazar - `bucket_map` zaten
    `_accumulate_departure_flow()` ile doldurulmuş, burada SADECE
    toplamlar (`total=domestic+schengen+non_schengen`) hesaplanıp satıra
    dönüştürülüyor. `calculation_method='departure_showup_cohort_sum'`
    - SQL'den bu run'ın hangi yöntemle üretildiği her satırda görülebilir."""
    rows: list[QueueAirportDepartureFlow5m] = []
    for window_start, bucket in sorted(bucket_map.items()):
        domestic = bucket["domestic"]
        schengen = bucket["schengen"]
        non_schengen = bucket["non_schengen"]
        total = domestic + schengen + non_schengen
        window_end = window_start + timedelta(minutes=DEPARTURE_SHOWUP_BUCKET_MINUTES)
        rows.append(QueueAirportDepartureFlow5m(
            run_id=run_id, airport_iata=airport_iata,
            window_start_utc=window_start, window_start_local=_to_local(window_start, tz),
            window_end_utc=window_end, window_end_local=_to_local(window_end, tz),
            total_departure_pax=total,
            domestic_departure_pax=domestic,
            international_departure_pax=schengen + non_schengen,
            schengen_departure_pax=schengen,
            non_schengen_departure_pax=non_schengen,
            flight_count=len(bucket["flight_keys"]),
            calculation_method="departure_showup_cohort_sum",
        ))
    _bulk_add(session, rows)


def _emit_departure_cohorts_and_contributions(
    cohort_rows, contribution_rows, common, detailed, process, tz,
    routing_source, routing_destination, security_arrival_source,
) -> None:
    by_segment_hour: dict[tuple, dict] = {}
    for cohort_time, count, (minutes_before_start, minutes_before_end, fraction) in detailed:
        cohort_rows.append(QueueCohortAudit(
            run_id=common["run_id"], airport_iata=common["airport_iata"], process=process,
            flight_key=common["flight_key"], flight_iata=common["flight_iata"], direction=common["direction"],
            profile_segment=f"T-{minutes_before_start}->T-{minutes_before_end}", profile_percentage=fraction * 100,
            aircraft_capacity=common["resolved_aircraft_capacity"],
            cohort_start_utc=cohort_time, cohort_start_local=_to_local(cohort_time, tz),
            cohort_resolution_minutes=DEPARTURE_SHOWUP_BUCKET_MINUTES,
            passenger_count=count,
            routing_stage="departure_show_up", source_process=None, destination_process=routing_destination,
        ))
        hour = _floor_hour(cohort_time)
        seg_label = f"T-{minutes_before_start}->T-{minutes_before_end}"
        key = (seg_label, hour)
        bucket = by_segment_hour.setdefault(key, {"passengers": 0.0, "percentage": fraction * 100})
        bucket["passengers"] += count

    segment_totals: dict[str, float] = defaultdict(float)
    for (seg_label, _hour), bucket in by_segment_hour.items():
        segment_totals[seg_label] += bucket["passengers"]

    for (seg_label, hour), bucket in sorted(by_segment_hour.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        contribution_rows.append(QueueFlightHourContributionAudit(
            **common, process=process,
            window_start_utc=hour, window_start_local=_to_local(hour, tz),
            window_end_local=_to_local(hour + timedelta(hours=1), tz),
            profile_name="departure_show_up", profile_segment=seg_label, profile_percentage=bucket["percentage"],
            segment_start_utc=None, segment_end_utc=None,
            passengers_from_segment=segment_totals[seg_label],
            passengers_contributed_to_this_hour=bucket["passengers"],
            routing_source=routing_source, routing_destination=routing_destination,
            security_arrival_source=security_arrival_source,
        ))


def _emit_arrival_cohorts_and_contributions(
    cohort_rows, contribution_rows, common, detailed, process, tz,
    routing_source, routing_destination, security_arrival_source,
) -> None:
    by_segment_hour: dict[tuple, dict] = {}
    for cohort_time, count, (minutes_after_start, minutes_after_end, fraction) in detailed:
        cohort_rows.append(QueueCohortAudit(
            run_id=common["run_id"], airport_iata=common["airport_iata"], process=process,
            flight_key=common["flight_key"], flight_iata=common["flight_iata"], direction=common["direction"],
            profile_segment=f"T+{minutes_after_start}->T+{minutes_after_end}", profile_percentage=fraction * 100,
            aircraft_capacity=common["resolved_aircraft_capacity"],
            cohort_start_utc=cohort_time, cohort_start_local=_to_local(cohort_time, tz),
            cohort_resolution_minutes=ARRIVAL_RELEASE_BUCKET_MINUTES,
            passenger_count=count,
            routing_stage="arrival_release", source_process=None, destination_process=routing_destination,
        ))
        hour = _floor_hour(cohort_time)
        seg_label = f"T+{minutes_after_start}->T+{minutes_after_end}"
        key = (seg_label, hour)
        bucket = by_segment_hour.setdefault(key, {"passengers": 0.0, "percentage": fraction * 100})
        bucket["passengers"] += count

    segment_totals: dict[str, float] = defaultdict(float)
    for (seg_label, _hour), bucket in by_segment_hour.items():
        segment_totals[seg_label] += bucket["passengers"]

    for (seg_label, hour), bucket in sorted(by_segment_hour.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        contribution_rows.append(QueueFlightHourContributionAudit(
            **common, process=process,
            window_start_utc=hour, window_start_local=_to_local(hour, tz),
            window_end_local=_to_local(hour + timedelta(hours=1), tz),
            profile_name="arrival_release", profile_segment=seg_label, profile_percentage=bucket["percentage"],
            segment_start_utc=None, segment_end_utc=None,
            passengers_from_segment=segment_totals[seg_label],
            passengers_contributed_to_this_hour=bucket["passengers"],
            routing_source=routing_source, routing_destination=routing_destination,
            security_arrival_source=security_arrival_source,
        ))


# ---------------------------------------------------------------------------
# Table 10 (per-physical-flight resolution snapshot)
# ---------------------------------------------------------------------------

def _routing_path_for(flight) -> str:
    if is_domestic_departure(flight):
        return "domestic_departure->security_dom"
    if _is_domestic_arrival(flight):
        return "domestic_arrival->none"
    if is_schengen_departure_skipping_passport(flight):
        return "schengen_departure->security_intl_direct"
    if is_international_departure_requiring_passport(flight):
        return "non_schengen_departure->passport_dep->security_intl"
    if _is_schengen_arrival_bypassing_passport(flight):
        return "schengen_arrival->bypass_passport"
    if is_international_arrival_requiring_passport(flight):
        return "non_schengen_arrival->passport_arr"
    return "unclassified"


def record_flight_resolution_audit(
    session, run_id: str, airport_iata: str, flights: list, demand, country_by_iata: dict[str, str],
) -> None:
    """Her fiziksel flight için TEK satır (Bölüm 1) - `passenger_demand()`
    zaten HER flight için ana hesaplama sırasında çağrıldığından, burada
    `demand.capacity_result(flight)` AYNI cache'ten okur (yeniden DB
    sorgusu/`_flag_unknown()` tetiklenmez, bkz. demand.py docstring'i)."""
    rows: list[QueueFlightResolutionAudit] = []
    for flight in flights:
        if flight.status in EXCLUDED_STATUSES:
            continue
        classification = _classify_flight(flight, country_by_iata)
        total_demand = demand.passenger_demand(flight)
        if total_demand <= 0:
            continue
        capacity = demand.capacity_result(flight)

        rows.append(QueueFlightResolutionAudit(
            run_id=run_id, airport_iata=airport_iata,
            flight_key=flight.flight_key, flight_iata=getattr(flight, "flight_iata", None),
            airline_iata=getattr(flight, "airline_iata", None), direction=flight.direction,
            dep_iata=flight.dep_iata, arr_iata=flight.arr_iata,
            dep_scheduled_utc=flight.dep_scheduled_utc, dep_estimated_utc=flight.dep_estimated_utc,
            dep_actual_utc=flight.dep_actual_utc, dep_effective_utc=_departure_show_up_base(flight),
            dep_time_source=_time_source_label(
                flight.dep_actual_utc, flight.dep_estimated_utc, flight.dep_scheduled_utc,
            ),
            arr_scheduled_utc=flight.arr_scheduled_utc, arr_estimated_utc=flight.arr_estimated_utc,
            arr_actual_utc=flight.arr_actual_utc, arr_effective_utc=_arrival_release_base(flight),
            arr_time_source=_time_source_label(
                flight.arr_actual_utc, flight.arr_estimated_utc, flight.arr_scheduled_utc,
            ),
            aircraft_icao=getattr(flight, "aircraft_icao", None),
            aircraft_match_found=getattr(flight, "aircraft_match_found", None),
            resolved_capacity=total_demand, capacity_source=capacity.source,
            capacity_confidence=capacity.confidence,
            is_domestic=classification["is_domestic"], is_international=classification["is_international"],
            is_schengen=classification["is_schengen"],
            requires_passport=getattr(flight, "requires_passport", True),
            routing_path=_routing_path_for(flight),
        ))
    _bulk_add(session, rows)


# ---------------------------------------------------------------------------
# Table 4 (ServiceEvent-level, real FIFO output)
# ---------------------------------------------------------------------------

def record_service_event_audit(
    session, run_id: str, airport_iata: str, coupling: dict, config, tz,
) -> None:
    resource_by_process = {
        PROCESS_SECURITY_DOMESTIC: (config.domestic_security_lane_count, config.security_service_time_minutes),
        PROCESS_SECURITY_INTL: (config.international_security_lane_count, config.security_service_time_minutes),
        PROCESS_PASSPORT_DEPARTURE: (config.passport_departure_server_count, config.passport_service_time_minutes),
        PROCESS_PASSPORT_ARRIVAL: (config.passport_arrival_server_count, config.passport_service_time_minutes),
    }
    rows: list[QueueServiceEventAudit] = []
    for process, (resource_count, service_time) in resource_by_process.items():
        events = coupling.get("process_events", {}).get(process, [])
        for event in events:
            shares = _flight_shares_of(event.source_flight_keys)
            rows.append(QueueServiceEventAudit(
                run_id=run_id, airport_iata=airport_iata, process=process,
                source_flight_keys=json.dumps(shares, sort_keys=True) if shares else None,
                arrival_time_utc=event.arrival_time, arrival_time_local=_to_local(event.arrival_time, tz),
                service_start_time_utc=event.service_start_time, service_start_time_local=_to_local(event.service_start_time, tz),
                completion_time_utc=event.completion_time, completion_time_local=_to_local(event.completion_time, tz),
                wait_minutes=event.wait_minutes, passenger_count=event.count,
                resource_count=resource_count, service_time_minutes=service_time,
                backlog_before=None,
            ))
    _bulk_add(session, rows)


# ---------------------------------------------------------------------------
# Table 1 (hourly calculation summary, from already-computed WindowPrediction)
# ---------------------------------------------------------------------------

_RESOURCE_INFO_BY_PROCESS = {
    PROCESS_SECURITY_DOMESTIC: "security_lane",
    PROCESS_SECURITY_INTL: "security_lane",
    PROCESS_PASSPORT_DEPARTURE: "passport_desk",
    PROCESS_PASSPORT_ARRIVAL: "passport_desk",
}


def record_hourly_calculation_audit(
    session, run_id: str, airport_iata: str, config, tz,
    predictions: list, coupling: dict, calculation_date: date_type | None,
) -> None:
    rows: list[QueueCalculationHourlyAudit] = []
    for prediction in predictions:
        if prediction.process not in _RESOURCE_INFO_BY_PROCESS:
            continue
        resource_type = _RESOURCE_INFO_BY_PROCESS[prediction.process]
        if resource_type == "security_lane":
            lane_or_desk = (
                config.domestic_security_lane_count if prediction.process == PROCESS_SECURITY_DOMESTIC
                else config.international_security_lane_count
            )
            service_time = config.security_service_time_minutes
        else:
            lane_or_desk = (
                config.passport_departure_server_count if prediction.process == PROCESS_PASSPORT_DEPARTURE
                else config.passport_arrival_server_count
            )
            service_time = config.passport_service_time_minutes
        capacity_per_resource = 60.0 / service_time if service_time else None
        total_capacity = capacity_per_resource * lane_or_desk if capacity_per_resource and lane_or_desk else None

        backlog_by_hour = coupling.get("backlog_start_by_hour", {}).get(prediction.process, {})
        backlog_start = backlog_by_hour.get(prediction.window_start)
        backlog_end = None
        if backlog_start is not None:
            backlog_end = backlog_by_hour.get(prediction.window_end, 0.0)

        rows.append(QueueCalculationHourlyAudit(
            run_id=run_id, airport_iata=airport_iata,
            airport_scale=config.scale, airport_timezone=str(tz) if tz else None,
            calculation_date=calculation_date,
            process=prediction.process,
            window_start_utc=prediction.window_start, window_start_local=_to_local(prediction.window_start, tz),
            window_end_utc=prediction.window_end, window_end_local=_to_local(prediction.window_end, tz),
            direction=(
                DIRECTION_ARRIVAL if prediction.process == PROCESS_PASSPORT_ARRIVAL
                else DIRECTION_DEPARTURE
            ),
            flight_count=prediction.flight_count, expected_passengers=prediction.expected_passengers,
            lane_or_desk_count=lane_or_desk, resource_type=resource_type, service_time_minutes=service_time,
            capacity_per_resource_per_hour=capacity_per_resource, total_hourly_capacity=total_capacity,
            backlog_start=backlog_start,
            backlog_end=backlog_end,
            estimated_wait_minutes=prediction.estimated_wait_minutes, risk=prediction.risk,
            display_status=prediction.risk,
        ))
    _bulk_add(session, rows)


# ---------------------------------------------------------------------------
# Table 6 + 7 (routing summary + country breakdown)
# ---------------------------------------------------------------------------

def record_routing_and_country_audit(
    session, run_id: str, airport_iata: str, flights: list, demand,
    country_by_iata: dict[str, str], calculation_date: date_type | None,
    codeshare_records_removed: int = 0,
) -> None:
    active_flights = [f for f in flights if f.status not in EXCLUDED_STATUSES]

    summary = dict(
        total_physical_flights=len(active_flights),
        domestic_departure_flights=0, international_departure_flights=0,
        domestic_arrival_flights=0, international_arrival_flights=0,
        schengen_departure_flights=0, non_schengen_departure_flights=0,
        schengen_arrival_flights=0, non_schengen_arrival_flights=0,
        schengen_departures_bypassed_passport=0, non_schengen_departures_entered_passport=0,
        schengen_arrivals_bypassed_passport=0, non_schengen_arrivals_entered_passport=0,
    )

    country_buckets: dict[tuple, dict] = {}

    for flight in active_flights:
        classification = _classify_flight(flight, country_by_iata)
        total_demand = demand.passenger_demand(flight)
        counterpart_country = (
            classification["arr_country"] if flight.direction == DIRECTION_DEPARTURE
            else classification["dep_country"]
        )

        if is_domestic_departure(flight):
            summary["domestic_departure_flights"] += 1
        elif is_international_departure(flight):
            summary["international_departure_flights"] += 1
            if is_schengen_departure_skipping_passport(flight):
                summary["schengen_departure_flights"] += 1
                summary["schengen_departures_bypassed_passport"] += 1
            elif is_international_departure_requiring_passport(flight):
                summary["non_schengen_departure_flights"] += 1
                summary["non_schengen_departures_entered_passport"] += 1
        elif _is_domestic_arrival(flight):
            summary["domestic_arrival_flights"] += 1
        elif is_international_arrival(flight):
            summary["international_arrival_flights"] += 1
            if _is_schengen_arrival_bypassing_passport(flight):
                summary["schengen_arrival_flights"] += 1
                summary["schengen_arrivals_bypassed_passport"] += 1
            elif is_international_arrival_requiring_passport(flight):
                summary["non_schengen_arrival_flights"] += 1
                summary["non_schengen_arrivals_entered_passport"] += 1

        flight_type = "domestic" if classification["is_domestic"] else "international"
        if classification["is_schengen"] is None:
            schengen_status = "n/a"
        elif classification["is_schengen"]:
            schengen_status = "schengen"
        else:
            schengen_status = "non_schengen" if classification["is_international"] else "n/a"

        key = (counterpart_country, flight.direction, flight_type, schengen_status)
        bucket = country_buckets.setdefault(key, {
            "flight_count": 0, "passenger_count": 0.0,
            "passport_required_count": 0, "passport_bypass_count": 0,
            "security_dom_passengers": 0.0, "passport_dep_passengers": 0.0,
            "security_intl_passengers": 0.0, "passport_arr_passengers": 0.0,
        })
        bucket["flight_count"] += 1
        bucket["passenger_count"] += total_demand
        requires_passport = getattr(flight, "requires_passport", True)
        if flight_type == "international":
            if requires_passport:
                bucket["passport_required_count"] += 1
            else:
                bucket["passport_bypass_count"] += 1

        if is_domestic_departure(flight):
            bucket["security_dom_passengers"] += total_demand
        elif is_schengen_departure_skipping_passport(flight):
            bucket["security_intl_passengers"] += total_demand
        elif is_international_departure_requiring_passport(flight):
            bucket["passport_dep_passengers"] += total_demand
            bucket["security_intl_passengers"] += total_demand
        elif is_international_arrival_requiring_passport(flight):
            bucket["passport_arr_passengers"] += total_demand

    session.add(QueueRoutingSummaryAudit(
        run_id=run_id, airport_iata=airport_iata, calculation_date=calculation_date,
        codeshare_records_removed=codeshare_records_removed,
        **summary,
    ))

    country_rows = []
    for (country_code, direction, flight_type, schengen_status), bucket in country_buckets.items():
        country_rows.append(QueueCountryRoutingAudit(
            run_id=run_id, airport_iata=airport_iata, calculation_date=calculation_date,
            country_code=country_code, country_name=COUNTRY_NAMES.get((country_code or "").upper()),
            direction=direction, flight_type=flight_type, schengen_status=schengen_status,
            **bucket,
        ))
    _bulk_add(session, country_rows)
    session.flush()


# ---------------------------------------------------------------------------
# Table 8 (dynamic passport staffing checkpoints)
# ---------------------------------------------------------------------------

def _dynamic_params_snapshot_by_process(config) -> dict:
    """
    ADIM (Editable Dynamic Staffing Config, Bölüm 19) - `queue_dynamic_
    staffing_audit`'in "kaç yolcuda kaça çıktı" sorusunu JOIN'siz
    cevaplayabilmesi için, her dynamic sürecin O ANDA GEÇERLİ (DB'den
    okunmuş) base/max/interval/service_time/target_utilization'ının bir
    anlık görüntüsü - `record_service_event_audit`'in `resource_by_
    process` deseniyle AYNI, sadece daha fazla alan taşıyor.
    """
    return {
        PROCESS_SECURITY_INTL: {
            "base": config.international_security_lane_count,
            "max": config.international_security_lane_count_max,
            "interval": config.security_dynamic_control_interval_minutes,
            "service_time": config.security_service_time_minutes,
            "target_utilization": config.dynamic_target_utilization,
        },
        PROCESS_PASSPORT_DEPARTURE: {
            "base": config.passport_departure_server_count,
            "max": config.passport_departure_server_count_max,
            "interval": config.passport_departure_control_interval_minutes,
            "service_time": config.passport_service_time_minutes,
            "target_utilization": config.dynamic_target_utilization,
        },
        PROCESS_PASSPORT_ARRIVAL: {
            "base": config.passport_arrival_server_count,
            "max": config.passport_arrival_server_count_max,
            "interval": config.passport_arrival_control_interval_minutes,
            "service_time": config.passport_service_time_minutes,
            "target_utilization": config.dynamic_target_utilization,
        },
    }


def record_dynamic_staffing_audit(
    session, run_id: str, airport_iata: str, coupling: dict, tz, config,
) -> None:
    # ADIM (Section 16 - backlog/lookahead/needed artık NULL değil):
    # `simulate_fifo_queue_dynamic()` artık `checkpoint_log`'u (Section
    # 17'deki `reason` dahil) production math'ten TAMAMEN AYRI, ek bir
    # dönüş değeri olarak expose ediyor (bkz. event_queue.py
    # `DynamicStaffingCheckpoint`) - queue/FIFO hesaplaması hiç
    # değişmedi, sadece zaten hesaplanan ara değerler dışarı sızdırıldı.
    checkpoint_logs = coupling.get("dynamic_checkpoint_logs", {})
    schedules = coupling.get("dynamic_schedules", {})
    snapshots = _dynamic_params_snapshot_by_process(config)
    rows = []
    for process, log in checkpoint_logs.items():
        if not log:
            continue
        snap = snapshots.get(process, {})
        service_time = snap.get("service_time")
        per_minute_rate = (1.0 / service_time) if service_time else None

        def _capacity(count):
            if per_minute_rate is None or count is None:
                return None, None
            per_minute = count * per_minute_rate
            return per_minute, per_minute * 60

        # İlk (başlangıç) satırı - `checkpoint_log` sadece GERÇEK `_apply_
        # checkpoint()` çağrılarını içerir; sürecin `default_server_count`
        # ile başladığı ANIN kendisi (schedule'ın ilk elemanı) ayrı olarak
        # ekleniyor - önceki (schedule-only) davranışla AYNI ilk satır.
        schedule = schedules.get(process)
        if schedule:
            first_time, first_count = sorted(schedule, key=lambda item: item[0])[0]
            cap_min, cap_hour = _capacity(first_count)
            rows.append(QueueDynamicStaffingAudit(
                run_id=run_id, airport_iata=airport_iata, process=process,
                checkpoint_time_utc=first_time, checkpoint_time_local=_to_local(first_time, tz),
                previous_server_count=None, new_server_count=first_count,
                backlog=None, lookahead_demand=None, needed_servers=None,
                ramp_delta=None, pending_retirements=None, reason="initial",
                base_resource_count=snap.get("base"), max_resource_count=snap.get("max"),
                total_workload=None, control_interval_minutes=snap.get("interval"),
                service_time_minutes=service_time, target_utilization=snap.get("target_utilization"),
                effective_capacity_per_minute=cap_min, effective_capacity_per_hour=cap_hour,
            ))
        for entry in sorted(log, key=lambda item: item.checkpoint_time):
            cap_min, cap_hour = _capacity(entry.new_count)
            total_workload = (
                entry.backlog + entry.lookahead_demand
                if entry.backlog is not None and entry.lookahead_demand is not None
                else None
            )
            rows.append(QueueDynamicStaffingAudit(
                run_id=run_id, airport_iata=airport_iata, process=process,
                checkpoint_time_utc=entry.checkpoint_time, checkpoint_time_local=_to_local(entry.checkpoint_time, tz),
                previous_server_count=entry.previous_count, new_server_count=entry.new_count,
                backlog=entry.backlog, lookahead_demand=entry.lookahead_demand,
                needed_servers=entry.needed_servers, ramp_delta=entry.ramp,
                pending_retirements=entry.pending_retirements_after,
                target_operational_level=entry.target_operational_level,
                reason=entry.reason,
                base_resource_count=snap.get("base"), max_resource_count=snap.get("max"),
                total_workload=total_workload, control_interval_minutes=snap.get("interval"),
                service_time_minutes=service_time, target_utilization=snap.get("target_utilization"),
                effective_capacity_per_minute=cap_min, effective_capacity_per_hour=cap_hour,
                normal_lookahead_demand=entry.normal_lookahead_demand,
                extended_lookahead_demand=entry.extended_lookahead_demand,
                normal_needed_servers=entry.normal_needed_servers,
                extended_needed_servers=entry.extended_needed_servers,
                winning_forecast=entry.winning_forecast,
            ))
    _bulk_add(session, rows)


# ---------------------------------------------------------------------------
# Table 9 (graph's own 30-minute visual aggregation, server-side snapshot)
# ---------------------------------------------------------------------------

def record_graph_display_audit(
    session, run_id: str, airport_iata: str, process: str,
    five_minute_points: list, tz, now: datetime,
) -> None:
    """`five_minute_points`: `event_driven_display_series()`'in ÜRETTİĞİ
    `FiveMinuteWaitPoint` listesi - ADIM (Current-Queue Weighted
    Remaining Wait): her nokta artık "o checkpoint anında hâlâ kuyrukta
    bekleyenlerin ortalama KALAN bekleme süresi" (`_remaining_wait_at_
    checkpoint`, eski arrival-window metriği/`virtual_arrival_wait`
    İKİSİ DE KALDIRILDI). 30dk bar 6 adet checkpoint'in basit ortalaması
    DEĞİL - o 30dk'daki TÜM 6 checkpoint'in numerator/passenger_count'
    larının TOPLANIP bölünmesi (`SUM(numerator)/SUM(pax)`) - formül
    (`_bucket_weighted_wait` ile AYNI desen) DEĞİŞMEDİ, sadece girdinin
    (`passenger_count`) anlamı değişti. Frontend'in kendi hesabını
    DEĞİŞTİRMEZ/OKUMAZ - sadece SUNUCU tarafında bir kez daha (salt
    gözlem için) hesaplayıp SQL'den görünür kılar."""
    from .core.scoring import risk_from_wait

    buckets: dict[datetime, dict] = defaultdict(
        lambda: {"numerator": 0.0, "passengers": 0.0, "source_points": 0, "peak": 0.0}
    )
    for point in five_minute_points:
        bucket_minute = 0 if point.window_start.minute < 30 else 30
        bucket_start = point.window_start.replace(minute=bucket_minute, second=0, microsecond=0)
        bucket = buckets[bucket_start]
        bucket["numerator"] += point.wait_numerator
        bucket["passengers"] += point.passenger_count
        bucket["source_points"] += 1
        bucket["peak"] = max(bucket["peak"], point.wait_minutes)

    current_bucket = None
    if five_minute_points:
        past = [p.window_start for p in five_minute_points if p.window_start <= now]
        if past:
            latest = max(past)
            bucket_minute = 0 if latest.minute < 30 else 30
            current_bucket = latest.replace(minute=bucket_minute, second=0, microsecond=0)

    rows = []
    for bucket_start, bucket in sorted(buckets.items()):
        passengers = bucket["passengers"]
        weighted_average = bucket["numerator"] / passengers if passengers > 0 else 0.0
        rows.append(QueueGraphDisplayAudit(
            run_id=run_id, airport_iata=airport_iata, process=process,
            bucket_start_utc=bucket_start, bucket_start_local=_to_local(bucket_start, tz),
            source_resolution_minutes=5, visual_bucket_minutes=30,
            source_point_count=bucket["source_points"],
            wait_numerator=bucket["numerator"], passenger_count=passengers,
            average_wait_minutes=weighted_average, peak_wait_minutes=bucket["peak"],
            display_wait_minutes=weighted_average,
            status=risk_from_wait(weighted_average),
            is_current=(bucket_start == current_bucket),
        ))
    _bulk_add(session, rows)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

_AUDIT_MODELS = (
    QueueCalculationHourlyAudit, QueueFlightHourContributionAudit, QueueCohortAudit,
    QueueServiceEventAudit, QueueRoutingSummaryAudit, QueueCountryRoutingAudit,
    QueueResourceConfigAudit, QueueDynamicStaffingAudit, QueueGraphDisplayAudit,
    QueueFlightResolutionAudit, QueueAirportDepartureFlow5m,
)


def purge_expired_audit_rows(session, retention_days: int | None = None) -> dict:
    """SADECE audit/debug tablolarına dokunur - Flight/QueuePrediction/
    QueueWaitDisplay5m/aircraft_capacity dahil HİÇBİR production
    tablosuna DOKUNMAZ."""
    days = audit_retention_days() if retention_days is None else retention_days
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    deleted = {}
    for model in _AUDIT_MODELS:
        result = session.query(model).filter(model.created_at < cutoff).delete(synchronize_session=False)
        deleted[model.__tablename__] = result
    session.commit()
    return deleted


def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m app.queue.audit",
        description=(
            "Queue calculation audit tabloları (queue_*_audit) için "
            "retention temizliği. Flight/QueuePrediction/QueueWaitDisplay5m "
            "dahil hiçbir production tablosuna DOKUNMAZ."
        ),
    )
    parser.add_argument(
        "--purge", action="store_true",
        help="QUEUE_AUDIT_RETENTION_DAYS'ten (varsayılan 7) eski audit satırlarını siler.",
    )
    parser.add_argument("--retention-days", type=int, default=None)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.purge:
        parser.print_help()
        return

    from ..db import get_session
    from ..logging_config import configure_logging

    configure_logging()
    session = get_session()
    try:
        result = purge_expired_audit_rows(session, retention_days=args.retention_days)
    finally:
        session.close()

    for table, count in result.items():
        print(f"{table}: {count} satır silindi")


if __name__ == "__main__":
    main()
