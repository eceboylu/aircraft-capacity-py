
from dataclasses import dataclass

from sqlalchemy import select

from .constants import (
    ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES,
    DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES,
    DYNAMIC_TARGET_UTILIZATION,
)
from .domain.airport_scale import resource_view_for_scale
from .models import Airport, AirportOperationalConfig, AirportScaleConfig

_CONFIG_FIELDS = (
    "passport_counter_count",
    "passport_staff_count",
    "passport_service_time_minutes",
    "security_lane_count",
    "domestic_security_lane_count",
    "international_security_lane_count",
    "security_service_time_minutes",
    "passport_staff_per_counter",
    "passport_service_rate_per_staff",
    "passport_efficiency_multiplier",
    "arrival_bank_threshold",
)


def _column_defaults() -> dict:
    defaults = {}
    for name in _CONFIG_FIELDS:
        column = AirportOperationalConfig.__table__.columns[name]
        defaults[name] = column.default.arg
    return defaults


def _legacy_unknown_server_count() -> int:
    defaults = _column_defaults()
    return round(defaults["passport_counter_count"] * defaults["passport_staff_per_counter"])


@dataclass(kw_only=True)
class AirportConfigView:

    airport_iata: str
    passport_counter_count: int
    passport_staff_count: int
    passport_service_time_minutes: float
    security_lane_count: int
    domestic_security_lane_count: int = 8
    international_security_lane_count: int = 8
    security_service_time_minutes: float
    passport_staff_per_counter: float
    passport_service_rate_per_staff: float
    passport_efficiency_multiplier: float
    arrival_bank_threshold: int
    is_default: bool
    passport_departure_server_count: int = 8
    passport_arrival_server_count: int = 8
    passport_departure_dynamic: bool = False
    passport_arrival_dynamic: bool = False
    passport_departure_server_count_max: int | None = None
    passport_arrival_server_count_max: int | None = None
    scale: str | None = None
    # ADIM (MEGA Dynamic Security) - security_intl artık (SADECE MEGA'da,
    # ve SADECE manuel override yoksa) domestic'ten BAĞIMSIZ olarak
    # dinamik olabiliyor. `international_security_lane_count` (yukarıda)
    # HER ZAMAN o anki EFEKTİF (base VEYA dynamic olarak açılmış) lane
    # sayısını taşımaya devam ediyor - bu alanlar SADECE dynamic
    # motorunu (var/yok, tavan ne, checkpoint aralığı ne) besliyor.
    security_intl_dynamic: bool = False
    international_security_lane_count_max: int | None = None
    security_dynamic_control_interval_minutes: int = DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    passport_dynamic_control_interval_minutes: int = DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    # ADIM (Editable Dynamic Staffing Config) - process-özel checkpoint
    # aralığı (DB'de özel kolon DOLUYSA onu, YOKSA yukarıdaki paylaşılan
    # `passport_dynamic_control_interval_minutes`'ı kullanır - bkz.
    # `_build_config_view`). `dynamic_target_utilization`/`passport_
    # arrival_lookahead_minutes` de aynı şekilde DB'den override
    # edilebilir, NULL ise `constants.py`'nin sabitlerine düşer.
    passport_departure_control_interval_minutes: int = DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    passport_arrival_control_interval_minutes: int = DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    dynamic_target_utilization: float = DYNAMIC_TARGET_UTILIZATION
    passport_arrival_lookahead_minutes: int = ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES


def _resolve_passport_server_counts(
    row: AirportOperationalConfig | None, resources: dict | None,
) -> tuple[int, int, bool, bool]:
    fallback = _legacy_unknown_server_count()
    scale_dep = resources["departure_passport_servers"] if resources else fallback
    scale_arr = resources["arrival_passport_servers"] if resources else fallback

    departure_is_override = row is not None and row.passport_departure_server_count is not None
    arrival_is_override = row is not None and row.passport_arrival_server_count is not None

    departure = row.passport_departure_server_count if departure_is_override else scale_dep
    arrival = row.passport_arrival_server_count if arrival_is_override else scale_arr

    return departure, arrival, departure_is_override, arrival_is_override


def _resolve_security_lane_counts(
    row: AirportOperationalConfig | None, resources: dict | None,
) -> tuple[int, int, bool]:
    """Üçüncü eleman: bu havalimanı için international security lane
    override'ı var mı (varsa dynamic KAPANIR - manuel override her zaman
    dynamic motorun ÖNÜNE geçer, passport'taki `*_is_override` deseniyle
    AYNI kural)."""
    if row is not None and not row.is_seeded_default:
        return row.domestic_security_lane_count, row.international_security_lane_count, True

    fallback = _legacy_unknown_server_count()
    domestic = resources["domestic_security_lanes"] if resources else fallback
    international = resources["international_security_lanes"] if resources else fallback
    return domestic, international, False


def _scale_resources_from_db(session) -> dict[str, dict]:
    rows = session.execute(select(AirportScaleConfig)).scalars().all()
    result: dict[str, dict] = {}
    for row in rows:
        entry = {
            "departure_passport_servers": row.departure_passport_servers,
            "arrival_passport_servers": row.arrival_passport_servers,
            "domestic_security_lanes": row.domestic_security_lanes,
            "international_security_lanes": row.international_security_lanes,
        }
        if row.departure_passport_servers_max is not None:
            entry["departure_passport_servers_max"] = row.departure_passport_servers_max
        if row.arrival_passport_servers_max is not None:
            entry["arrival_passport_servers_max"] = row.arrival_passport_servers_max
        if row.international_security_lanes_max is not None:
            entry["international_security_lanes_max"] = row.international_security_lanes_max
        if row.security_dynamic_control_interval_minutes is not None:
            entry["security_dynamic_control_interval_minutes"] = row.security_dynamic_control_interval_minutes
        if row.passport_dynamic_control_interval_minutes is not None:
            entry["passport_dynamic_control_interval_minutes"] = row.passport_dynamic_control_interval_minutes
        if row.passport_departure_control_interval_minutes is not None:
            entry["passport_departure_control_interval_minutes"] = row.passport_departure_control_interval_minutes
        if row.passport_arrival_control_interval_minutes is not None:
            entry["passport_arrival_control_interval_minutes"] = row.passport_arrival_control_interval_minutes
        if row.dynamic_target_utilization is not None:
            entry["dynamic_target_utilization"] = row.dynamic_target_utilization
        if row.passport_arrival_lookahead_minutes is not None:
            entry["passport_arrival_lookahead_minutes"] = row.passport_arrival_lookahead_minutes
        result[row.scale] = entry
    return result


def _build_config_view(
    airport_iata: str, row: AirportOperationalConfig | None, scale: str | None,
    db_scale_resources: dict[str, dict] | None = None,
) -> AirportConfigView:
    if db_scale_resources and scale in db_scale_resources:
        resources = db_scale_resources[scale]
    else:
        resources = resource_view_for_scale(scale)
    (
        departure_servers, arrival_servers,
        departure_is_override, arrival_is_override,
    ) = _resolve_passport_server_counts(row, resources)
    domestic_lanes, international_lanes, security_is_override = _resolve_security_lane_counts(row, resources)

    departure_max = resources.get("departure_passport_servers_max") if resources else None
    arrival_max = resources.get("arrival_passport_servers_max") if resources else None
    passport_departure_dynamic = not departure_is_override and departure_max is not None
    passport_arrival_dynamic = not arrival_is_override and arrival_max is not None

    international_security_max = resources.get("international_security_lanes_max") if resources else None
    security_intl_dynamic = not security_is_override and international_security_max is not None
    security_control_interval = (
        resources.get("security_dynamic_control_interval_minutes", DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES)
        if resources else DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    )
    passport_control_interval = (
        resources.get("passport_dynamic_control_interval_minutes", DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES)
        if resources else DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    )
    # ADIM (Editable Dynamic Staffing Config) - process-özel kolon
    # DOLUYSA öncelik onda, YOKSA yukarıdaki paylaşılan `passport_
    # control_interval`'a (o da NULL ise DEFAULT'a) düşülür - "SQL'den
    # sadece passport_dep interval'ı değiştir, arr AYNI kalsın" gibi
    # kısmi override'lar da desteklenir.
    passport_departure_control_interval = (
        resources.get("passport_departure_control_interval_minutes", passport_control_interval)
        if resources else passport_control_interval
    )
    passport_arrival_control_interval = (
        resources.get("passport_arrival_control_interval_minutes", passport_control_interval)
        if resources else passport_control_interval
    )
    dynamic_target_utilization = (
        resources.get("dynamic_target_utilization", DYNAMIC_TARGET_UTILIZATION)
        if resources else DYNAMIC_TARGET_UTILIZATION
    )
    passport_arrival_lookahead = (
        resources.get("passport_arrival_lookahead_minutes", ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES)
        if resources else ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES
    )

    # ADIM (Editable Dynamic Staffing Config) - Bölüm 23: phpMyAdmin'den
    # SQL'de geçersiz bir değer (`interval=0`, `target_utilization=0`
    # veya `>1`, negatif lookahead) girilirse SESSİZCE ilgili sabite
    # düşülür - runtime hiçbir zaman `ValueError`/bozuk bir checkpoint
    # ÜRETMEZ ("fail-safe", `event_queue.py`'nin geri kalanıyla AYNI
    # savunmacı üslup - bkz. `effective_levels` boşsa `[default]`'a
    # düşen desen).
    if not isinstance(security_control_interval, int) or security_control_interval <= 0:
        security_control_interval = DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    if not isinstance(passport_departure_control_interval, int) or passport_departure_control_interval <= 0:
        passport_departure_control_interval = DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    if not isinstance(passport_arrival_control_interval, int) or passport_arrival_control_interval <= 0:
        passport_arrival_control_interval = DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES
    if not isinstance(dynamic_target_utilization, (int, float)) or not (0 < dynamic_target_utilization <= 1):
        dynamic_target_utilization = DYNAMIC_TARGET_UTILIZATION
    if not isinstance(passport_arrival_lookahead, int) or passport_arrival_lookahead <= 0:
        passport_arrival_lookahead = ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES

    if row is not None:
        base_fields = {name: getattr(row, name) for name in _CONFIG_FIELDS}
        is_default = row.is_seeded_default
    else:
        base_fields = _column_defaults()
        is_default = True

    base_fields["domestic_security_lane_count"] = domestic_lanes
    base_fields["international_security_lane_count"] = international_lanes

    return AirportConfigView(
        airport_iata=airport_iata,
        is_default=is_default,
        passport_departure_dynamic=passport_departure_dynamic,
        passport_arrival_dynamic=passport_arrival_dynamic,
        passport_departure_server_count_max=departure_max if passport_departure_dynamic else None,
        passport_arrival_server_count_max=arrival_max if passport_arrival_dynamic else None,
        passport_departure_server_count=departure_servers,
        passport_arrival_server_count=arrival_servers,
        security_intl_dynamic=security_intl_dynamic,
        international_security_lane_count_max=international_security_max if security_intl_dynamic else None,
        security_dynamic_control_interval_minutes=security_control_interval,
        passport_dynamic_control_interval_minutes=passport_control_interval,
        passport_departure_control_interval_minutes=passport_departure_control_interval,
        passport_arrival_control_interval_minutes=passport_arrival_control_interval,
        dynamic_target_utilization=dynamic_target_utilization,
        passport_arrival_lookahead_minutes=passport_arrival_lookahead,
        scale=scale,
        **base_fields,
    )


def default_config(airport_iata: str, scale: str | None = None) -> AirportConfigView:
    return _build_config_view(airport_iata, row=None, scale=scale)


def get_config(session, airport_iata: str) -> AirportConfigView:
    row = session.get(AirportOperationalConfig, airport_iata)
    airport = session.get(Airport, airport_iata)
    scale = airport.scale if airport is not None else None
    db_scale_resources = _scale_resources_from_db(session)
    return _build_config_view(airport_iata, row=row, scale=scale, db_scale_resources=db_scale_resources)


def get_configs(session, airport_codes) -> dict[str, AirportConfigView]:
    codes = list(airport_codes)
    if not codes:
        return {}

    rows = session.execute(
        select(AirportOperationalConfig).where(
            AirportOperationalConfig.airport_iata.in_(codes)
        )
    ).scalars().all()
    rows_by_code = {row.airport_iata: row for row in rows}

    scales = session.execute(
        select(Airport.iata_code, Airport.scale).where(Airport.iata_code.in_(codes))
    ).all()
    scale_by_code = {iata: scale for iata, scale in scales}

    db_scale_resources = _scale_resources_from_db(session)

    return {
        code: _build_config_view(
            code, rows_by_code.get(code), scale_by_code.get(code),
            db_scale_resources=db_scale_resources,
        )
        for code in codes
    }
