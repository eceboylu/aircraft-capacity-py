
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable
import json
import logging

from sqlalchemy import select, tuple_

from .baseline import existing_baseline_observation_keys, record_observation
from .config import AirportConfigView, get_configs
from .constants import (
    DEMAND_WINDOW_MINUTES,
    DYNAMIC_RAMP_STEP,
    EXCLUDED_STATUSES,
    NO_BASELINE_MESSAGE,
    PASSPORT_ARR_OPERATIONAL_LEVELS,
    PASSPORT_DEP_OPERATIONAL_LEVELS,
    PASSPORT_OVERLOAD_MESSAGE,
    SECURITY_INTL_OPERATIONAL_LEVELS,
    PROCESS_PASSPORT,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    REASON_CAPACITY_EXCEEDED,
    REASON_NO_BASELINE,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    WAIT_DISPLAY_BUCKET_MINUTES,
)
from .core.event_queue import (
    DynamicStaffingParams,
    simulate_passport,
    simulate_security,
)
from .domain.dynamic_staffing import effective_capacity_by_hour
from .domain.operational_day import (
    filter_flights_for_operational_day,
    operational_date,
    operational_day_window,
    resolve_airport_timezone,
)
from .domain.retention_time import USAGE_HORIZON_HOURS, canonical_flight_time
from .core.scoring import (
    confidence_score,
    domestic_security_capacity_rate,
    international_security_capacity_rate,
    passport_arrival_capacity_rate,
    passport_arrival_server_count,
    passport_capacity_rate,
    passport_departure_capacity_rate,
    passport_departure_server_count,
    passport_effective_server_count,
    passport_queue_model,
    risk_from_wait,
    security_capacity_rate,
    security_density_score,
    security_queue_model,
)
from .domain.demand import (
    DemandCalculator,
    arrival_passenger_release_events,
    departure_show_up_events,
    effective_time,
    flights_in_window,
)
from .domain.flows import (
    passport_arrival_flights,
    passport_departure_flights,
    passport_flights,
    security_domestic_flights,
    security_flights,
    security_international_flights,
)
from .models import Airport, Flight, HistoricalFlightCount, QueuePrediction, QueueWaitDisplay5m
from .reasons.detector import DetectedReason, detect_reasons

logger = logging.getLogger(__name__)

PROCESSES = (
    PROCESS_SECURITY, PROCESS_PASSPORT,
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
)

_PROCESS_FLIGHTS = {
    PROCESS_SECURITY: security_flights,
    PROCESS_PASSPORT: passport_flights,
    PROCESS_SECURITY_DOMESTIC: security_domestic_flights,
    PROCESS_SECURITY_INTL: security_international_flights,
    PROCESS_PASSPORT_DEPARTURE: passport_departure_flights,
    PROCESS_PASSPORT_ARRIVAL: passport_arrival_flights,
}

_PASSPORT_POOL_BY_PROCESS = {
    PROCESS_PASSPORT: None,
    PROCESS_PASSPORT_DEPARTURE: "departure",
    PROCESS_PASSPORT_ARRIVAL: "arrival",
}

_EVENT_DRIVEN_WAIT_PROCESSES = frozenset({
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
})


@dataclass
class WindowPrediction:

    airport_iata: str
    process: str
    window_start: datetime
    window_end: datetime
    flight_count: int
    expected_passengers: int
    baseline_ratio: float | None
    utilization: float | None
    estimated_wait_minutes: float | None
    risk: str
    confidence: float
    flight_ratio: float | None = None
    passenger_ratio: float | None = None
    reasons: list[DetectedReason] = field(default_factory=list)
    operational_date: date | None = None

    def reasons_as_dicts(self) -> list[dict]:
        return [r.to_dict() for r in self.reasons]

    def reasons_json(self) -> str:
        return json.dumps(self.reasons_as_dicts(), ensure_ascii=False)


def domain_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def floor_to_window(
    moment: datetime, window_minutes: int = DEMAND_WINDOW_MINUTES
) -> datetime:
    minute = (moment.minute // window_minutes) * window_minutes
    return moment.replace(minute=minute, second=0, microsecond=0)


def window_starts(
    flights, window_minutes: int = DEMAND_WINDOW_MINUTES
) -> list[datetime]:
    starts = set()
    for flight in flights:
        moment = effective_time(flight)
        if moment is not None:
            starts.add(floor_to_window(moment, window_minutes))
    return sorted(starts)


def window_label(window_start: datetime, window_end: datetime) -> str:
    return f"{window_start:%H:%M}-{window_end:%H:%M}"


def _scoring_notes(score: dict) -> list[DetectedReason]:
    notes = []
    for message in score.get("reasons", []):
        if message == NO_BASELINE_MESSAGE:
            notes.append(DetectedReason(
                code=REASON_NO_BASELINE,
                severity=SEVERITY_INFO,
                message=NO_BASELINE_MESSAGE,
                metric_value=0.0,
            ))
        elif message == PASSPORT_OVERLOAD_MESSAGE:
            notes.append(DetectedReason(
                code=REASON_CAPACITY_EXCEEDED,
                severity=SEVERITY_CRITICAL,
                message=PASSPORT_OVERLOAD_MESSAGE,
                metric_value=score.get("utilization") or 0.0,
            ))
    return notes


def _bucket_flights_by_window(
    flights, window_minutes: int = DEMAND_WINDOW_MINUTES
) -> dict[datetime, list]:
    buckets: dict[datetime, list] = {}
    for flight in flights:
        moment = effective_time(flight)
        if moment is None:
            continue
        key = floor_to_window(moment, window_minutes)
        buckets.setdefault(key, []).append(flight)
    return buckets


def _predict_window_core(
    airport_iata: str,
    process: str,
    window_start: datetime,
    window_flights: list,
    window_all: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    historical_baseline: float | None,
    aircraft_match_rate: float,
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    historical_passenger_baseline: float | None = None,
    backlog_start: float = 0.0,
    now: datetime | None = None,
    demand_override: float | None = None,
    current_arrived_override: float | None = None,
    lane_count_override: int | None = None,
    event_driven_wait_override: float | None = None,
    risk_backlog_start: float = 0.0,
    server_count_override: float | None = None,
) -> WindowPrediction:
    window_end = window_start + timedelta(minutes=window_minutes)

    _now = now if now is not None else domain_now()
    elapsed_minutes = max(0.0, min(
        (_now - window_start).total_seconds() / 60.0, window_minutes
    ))
    if current_arrived_override is not None:
        current_arrived_demand = current_arrived_override
    else:
        current_arrived_demand = sum(
            demand.passenger_demand(f) for f in window_flights
            if (t := effective_time(f)) is not None and t <= _now
        )

    if process in _PASSPORT_POOL_BY_PROCESS:
        score = passport_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes,
            backlog_start=backlog_start,
            current_arrived_demand=current_arrived_demand,
            elapsed_minutes=elapsed_minutes,
            demand_override=demand_override,
            pool=_PASSPORT_POOL_BY_PROCESS[process],
            risk_backlog_start=risk_backlog_start,
            server_count_override=server_count_override,
        )
    else:
        score = security_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes,
            backlog_start=backlog_start,
            current_arrived_demand=current_arrived_demand,
            elapsed_minutes=elapsed_minutes,
            demand_override=demand_override,
            lane_count_override=lane_count_override,
            risk_backlog_start=risk_backlog_start,
        )
        density = security_density_score(
            window_flights,
            historical_baseline,
            demand.passenger_demand,
            historical_passenger_baseline,
        )
        score["baseline_ratio"] = density.get("baseline_ratio")
        score["flight_ratio"] = density.get("flight_ratio")
        score["passenger_ratio"] = density.get("passenger_ratio")
        score["reasons"] = score.get("reasons", []) + density.get("reasons", [])

    rho = score["utilization"]

    if event_driven_wait_override is not None:
        score["estimated_wait_minutes"] = round(event_driven_wait_override, 1)

    score["risk"] = risk_from_wait(score["estimated_wait_minutes"])

    window_changes: dict[str, list[tuple[str | None, str | None]]] | None = None
    if aircraft_changes:
        window_changes = {}
        for key, change_list in aircraft_changes.items():
            matching = [
                (old_icao, new_icao)
                for old_icao, new_icao, event_time in change_list
                if event_time is not None
                and window_start <= event_time < window_end
            ]
            if matching:
                window_changes[key] = matching

    reasons = _scoring_notes(score)
    reasons.extend(detect_reasons(
        airport_iata=airport_iata,
        process=process,
        window_flights=window_flights,
        all_period_flights=window_all,
        historical_baseline=historical_baseline,
        rho=rho,
        config=config,
        seat_capacity_fn=demand.seat_capacity,
        demand_fn=demand.passenger_demand,
        window_label=window_label(window_start, window_end),
        aircraft_changes=window_changes,
        capacity_of_icao=demand.capacity_of_icao,
    ))

    confidence = confidence_score(
        window_flights=window_flights,
        historical_baseline_available=historical_baseline is not None,
        aircraft_match_rate=aircraft_match_rate,
        config_is_default=config.is_default,
    )

    return WindowPrediction(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        window_end=window_end,
        flight_count=score["flight_count"],
        expected_passengers=score["expected_passengers"],
        baseline_ratio=score.get("baseline_ratio"),
        utilization=rho,
        estimated_wait_minutes=score["estimated_wait_minutes"],
        risk=score["risk"],
        confidence=confidence,
        flight_ratio=score.get("flight_ratio"),
        passenger_ratio=score.get("passenger_ratio"),
        reasons=reasons,
    )


def predict_window(
    airport_iata: str,
    process: str,
    window_start: datetime,
    process_flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    historical_baseline: float | None,
    aircraft_match_rate: float,
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    historical_passenger_baseline: float | None = None,
    backlog_start: float = 0.0,
    now: datetime | None = None,
) -> WindowPrediction:
    window_flights = flights_in_window(
        process_flights, window_start, window_minutes
    )
    window_all = flights_in_window(
        process_flights, window_start, window_minutes, include_excluded=True
    )
    return _predict_window_core(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        window_flights=window_flights,
        window_all=window_all,
        config=config,
        demand=demand,
        historical_baseline=historical_baseline,
        aircraft_match_rate=aircraft_match_rate,
        aircraft_changes=aircraft_changes,
        window_minutes=window_minutes,
        historical_passenger_baseline=historical_passenger_baseline,
        backlog_start=backlog_start,
        now=now,
    )


def _queue_backlog_starts(
    buckets: dict[datetime, list],
    capacity_rate: float,
    demand_fn,
    window_minutes: int,
) -> dict[datetime, float]:
    starts = sorted(buckets)
    if not starts:
        return {}

    service_capacity = capacity_rate * window_minutes

    backlog_starts: dict[datetime, float] = {}
    backlog = 0.0
    current = starts[0]
    last = starts[-1]
    step = timedelta(minutes=window_minutes)

    while current <= last:
        window_all = buckets.get(current, [])
        window_flights = [
            f for f in window_all if f.status not in EXCLUDED_STATUSES
        ]
        demand = sum(demand_fn(f) for f in window_flights)

        if current in buckets:
            backlog_starts[current] = backlog

        backlog = max(0.0, backlog + demand - service_capacity)

        current += step

    return backlog_starts


_MAX_BACKLOG_DRAIN_EXTENSION_HOURS = 24 * 30


def _hourly_backlog_chain(
    demand_by_hour: dict[datetime, float],
    capacity_rate: float | Callable[[datetime], float],
    window_minutes: int,
) -> dict[datetime, float]:
    starts = sorted(demand_by_hour)
    if not starts:
        return {}

    capacity_rate_fn = capacity_rate if callable(capacity_rate) else (lambda _t: capacity_rate)
    backlog_start: dict[datetime, float] = {}

    backlog = 0.0
    current = starts[0]
    last = starts[-1]
    step = timedelta(minutes=window_minutes)
    extension_hours = 0

    while current <= last or backlog > 0:
        if current > last:
            if extension_hours >= _MAX_BACKLOG_DRAIN_EXTENSION_HOURS:
                break
            extension_hours += 1
        demand_here = demand_by_hour.get(current, 0.0)
        service_capacity = capacity_rate_fn(current) * window_minutes
        backlog_start[current] = backlog
        backlog = max(0.0, backlog + demand_here - service_capacity)
        current += step

    return backlog_start


def _event_derived_backlog_by_hour(events, starts) -> dict[datetime, float]:
    return {
        start: sum(
            e.count for e in events
            if e.arrival_time < start and e.service_start_time > start
        )
        for start in starts
    }


def _operational_levels_for(
    default_count: int | None, maximum: int | None, fallback_levels: tuple[int, ...],
) -> tuple[int, ...]:
    """
    ADIM (SQL-Editable Operational Levels) - seviyeler artık (mümkünse)
    `base`/`max`'tan runtime'da TÜRETİLİR (base, base+5, ..., max) -
    "SQL'den max=35 yaparsam 40 hiç kullanılamasın" isteği, AYRI bir
    "levels" kolonu AÇMADAN bu formülle karşılanır (Bölüm 17).

    Geçersiz/tutarsız bir DB durumunda (max < base, ya da aradaki fark
    `DYNAMIC_RAMP_STEP`'in (5) tam katı DEĞİLSE - ör. max=37) GÜVENLİ
    TARAFTA kalınır: sabit `fallback_levels` (kod-içi, testli liste)
    AYNEN kullanılır - hiçbir zaman boş/hatalı bir seviye listesi
    ÜRETİLMEZ (Bölüm 23 - "step invalid rejected").
    """
    step = DYNAMIC_RAMP_STEP
    if (
        default_count is None or maximum is None
        or maximum < default_count
        or (maximum - default_count) % step != 0
    ):
        return fallback_levels
    return tuple(range(default_count, maximum + 1, step))


def _dynamic_staffing_params_for(
    config: AirportConfigView, pool: str,
) -> DynamicStaffingParams | None:
    """`pool`: "departure" | "arrival" (passport) | "security_intl".
    Control interval/look-ahead/target_utilization artık DB-driven
    (`config.*_control_interval_minutes`/`dynamic_target_utilization`/
    `passport_arrival_lookahead_minutes`) - eski hardcoded sabitler
    SADECE "DB'de değer YOKSA" fallback'i olarak kalır (bkz. config.py
    `_build_config_view`). `pool="arrival"` için ek olarak `extended_
    look_ahead_minutes` set edilir (Bölüm 11 - proaktif, backlog
    oluşmadan ÖNCE gören uzun pencere); diğer iki pool'da bu `None`
    kalır, davranışları BİREBİR aynı kalır."""
    if pool == "departure":
        enabled = config.passport_departure_dynamic
        default = config.passport_departure_server_count
        maximum = config.passport_departure_server_count_max
        control_interval = config.passport_departure_control_interval_minutes
        extended_look_ahead = None
        allowed_levels = _operational_levels_for(default, maximum, PASSPORT_DEP_OPERATIONAL_LEVELS)
    elif pool == "arrival":
        enabled = config.passport_arrival_dynamic
        default = config.passport_arrival_server_count
        maximum = config.passport_arrival_server_count_max
        control_interval = config.passport_arrival_control_interval_minutes
        extended_look_ahead = config.passport_arrival_lookahead_minutes
        allowed_levels = _operational_levels_for(default, maximum, PASSPORT_ARR_OPERATIONAL_LEVELS)
    else:
        enabled = config.security_intl_dynamic
        default = config.international_security_lane_count
        maximum = config.international_security_lane_count_max
        control_interval = config.security_dynamic_control_interval_minutes
        extended_look_ahead = None
        allowed_levels = _operational_levels_for(default, maximum, SECURITY_INTL_OPERATIONAL_LEVELS)

    if not enabled or maximum is None:
        return None

    return DynamicStaffingParams(
        default_server_count=default,
        max_server_count=maximum,
        control_interval_minutes=control_interval,
        look_ahead_minutes=control_interval,
        target_utilization=config.dynamic_target_utilization,
        ramp_step=DYNAMIC_RAMP_STEP,
        allowed_levels=allowed_levels,
        extended_look_ahead_minutes=extended_look_ahead,
    )


def _event_driven_queue_demand(
    flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    now: datetime | None = None,
) -> dict:
    from .domain.flows import (
        is_international_arrival,
        is_international_departure,
        is_schengen_departure_skipping_passport,
    )

    _now = now if now is not None else domain_now()

    def _arrival_release_arrivals(flight_list, predicate=None) -> list[tuple[datetime, float, str]]:
        result = []
        for f in flight_list:
            if f.status in EXCLUDED_STATUSES:
                continue
            if predicate is not None and not predicate(f):
                continue
            total = demand.passenger_demand(f)
            result.extend(
                (t, c, f.flight_key) for t, c in arrival_passenger_release_events(f, total)
            )
        return result

    def _departure_show_up_arrivals(flight_list, predicate=None) -> list[tuple[datetime, float, str]]:
        result = []
        for f in flight_list:
            if f.status in EXCLUDED_STATUSES:
                continue
            if predicate is not None and not predicate(f):
                continue
            total = demand.passenger_demand(f)
            result.extend(
                (t, c, f.flight_key) for t, c in departure_show_up_events(f, total)
            )
        return result

    departure_arrivals = _departure_show_up_arrivals(passport_flights(flights), is_international_departure)
    arrival_arrivals = _arrival_release_arrivals(passport_flights(flights), is_international_arrival)
    domestic_arrivals = _departure_show_up_arrivals(security_domestic_flights(flights))

    schengen_direct_security_arrivals = _departure_show_up_arrivals(
        flights, is_schengen_departure_skipping_passport
    )

    passport_result = simulate_passport(
        departure_arrivals,
        arrival_arrivals,
        passport_departure_server_count(config),
        passport_arrival_server_count(config),
        config.passport_service_time_minutes,
        departure_dynamic=_dynamic_staffing_params_for(config, pool="departure"),
        arrival_dynamic=_dynamic_staffing_params_for(config, pool="arrival"),
    )

    security_intl_arrivals = [
        (event.completion_time, event.count, event.source_flight_keys)
        for event in passport_result["departure"]
    ] + schengen_direct_security_arrivals
    security_intl_dynamic_params = _dynamic_staffing_params_for(config, pool="security_intl")
    security_intl_schedule = None
    security_intl_checkpoint_log = None
    if security_intl_dynamic_params is not None:
        security_intl_events, security_intl_schedule, security_intl_checkpoint_log = simulate_security(
            security_intl_arrivals,
            config.international_security_lane_count,
            config.security_service_time_minutes,
            origin=PROCESS_SECURITY_INTL,
            dynamic=security_intl_dynamic_params,
        )
    else:
        security_intl_events = simulate_security(
            security_intl_arrivals,
            config.international_security_lane_count,
            config.security_service_time_minutes,
            origin=PROCESS_SECURITY_INTL,
        )

    # Domestic security VE combined "security" özet süreci - Bölüm 6:
    # domestic bu turda BİLEREK static kalıyor, dynamic YOK.
    security_dom_events = simulate_security(
        domestic_arrivals,
        config.domestic_security_lane_count,
        config.security_service_time_minutes,
        origin=PROCESS_SECURITY_DOMESTIC,
    )

    security_combined_events = simulate_security(
        domestic_arrivals + security_intl_arrivals,
        config.security_lane_count,
        config.security_service_time_minutes,
        origin=PROCESS_SECURITY,
    )

    def _bucket_by_arrival(events) -> dict[datetime, float]:
        buckets: dict[datetime, float] = {}
        for event in events:
            key = floor_to_window(event.arrival_time, window_minutes)
            buckets[key] = buckets.get(key, 0.0) + event.count
        return buckets

    def _bucket_weighted_wait(events) -> dict[datetime, float]:
        weighted_sum: dict[datetime, float] = {}
        passenger_count: dict[datetime, float] = {}
        for event in events:
            key = floor_to_window(event.arrival_time, window_minutes)
            weighted_sum[key] = weighted_sum.get(key, 0.0) + event.wait_minutes * event.count
            passenger_count[key] = passenger_count.get(key, 0.0) + event.count
        return {
            key: weighted_sum[key] / passenger_count[key]
            for key in weighted_sum
            if passenger_count[key] > 0
        }

    def _bucket_current_released(events) -> dict[datetime, float]:
        buckets: dict[datetime, float] = {}
        for event in events:
            if event.arrival_time > _now:
                continue
            key = floor_to_window(event.arrival_time, window_minutes)
            buckets[key] = buckets.get(key, 0.0) + event.count
        return buckets

    process_events = {
        PROCESS_PASSPORT: passport_result["departure"] + passport_result["arrival"],
        PROCESS_PASSPORT_DEPARTURE: passport_result["departure"],
        PROCESS_PASSPORT_ARRIVAL: passport_result["arrival"],
        PROCESS_SECURITY_DOMESTIC: security_dom_events,
        PROCESS_SECURITY: security_combined_events,
        PROCESS_SECURITY_INTL: security_intl_events,
    }

    demand_by_hour = {
        process: _bucket_by_arrival(events)
        for process, events in process_events.items()
    }

    capacity_rates: dict[str, float | Callable[[datetime], float]] = {
        PROCESS_PASSPORT: passport_capacity_rate(config),
        PROCESS_PASSPORT_DEPARTURE: passport_departure_capacity_rate(config),
        PROCESS_PASSPORT_ARRIVAL: passport_arrival_capacity_rate(config),
        PROCESS_SECURITY_DOMESTIC: domestic_security_capacity_rate(config),
        PROCESS_SECURITY: security_capacity_rate(config),
        PROCESS_SECURITY_INTL: international_security_capacity_rate(config),
    }

    def _dynamic_capacity_rate_fn(
        schedule: list[tuple[datetime, int]], service_time_minutes: float,
    ) -> Callable[[datetime], float]:
        service_rate_per_server_per_minute = 1.0 / service_time_minutes
        ordered = sorted(schedule, key=lambda item: item[0])

        def _rate(hour_start: datetime) -> float:
            eff = effective_capacity_by_hour(ordered, [hour_start], window_minutes)
            active = eff.get(hour_start, ordered[0][1])
            return active * service_rate_per_server_per_minute

        return _rate

    departure_schedule = passport_result.get("departure_schedule")
    if departure_schedule is not None:
        capacity_rates[PROCESS_PASSPORT_DEPARTURE] = _dynamic_capacity_rate_fn(
            departure_schedule, config.passport_service_time_minutes
        )
    arrival_schedule = passport_result.get("arrival_schedule")
    if arrival_schedule is not None:
        capacity_rates[PROCESS_PASSPORT_ARRIVAL] = _dynamic_capacity_rate_fn(
            arrival_schedule, config.passport_service_time_minutes
        )
    if security_intl_schedule is not None:
        capacity_rates[PROCESS_SECURITY_INTL] = _dynamic_capacity_rate_fn(
            security_intl_schedule, config.security_service_time_minutes
        )

    backlog_start_by_hour = {
        process: _hourly_backlog_chain(
            demand_by_hour[process], capacity_rates[process], window_minutes
        )
        for process in demand_by_hour
    }

    current_released_by_hour = {
        process: _bucket_current_released(events)
        for process, events in process_events.items()
    }

    event_wait_by_hour = {
        process: _bucket_weighted_wait(events)
        for process, events in process_events.items()
    }

    def _bucket_contributing_flight_keys(events) -> dict[datetime, frozenset[str]]:
        buckets: dict[datetime, frozenset[str]] = {}
        for event in events:
            if not event.source_flight_keys:
                continue
            key = floor_to_window(event.arrival_time, window_minutes)
            buckets[key] = buckets.get(key, frozenset()) | frozenset(event.source_flight_keys)
        return buckets

    contributing_flight_keys_by_hour = {
        process: _bucket_contributing_flight_keys(events)
        for process, events in process_events.items()
    }

    return {
        "demand_by_hour": demand_by_hour,
        "backlog_start_by_hour": backlog_start_by_hour,
        "current_released_by_hour": current_released_by_hour,
        "event_wait_by_hour": event_wait_by_hour,
        "contributing_flight_keys_by_hour": contributing_flight_keys_by_hour,
        "process_events": process_events,
        # ADIM (MEGA Dynamic Security) - eskiden sadece passport'un iki
        # havuzunu taşıyordu ("passport_schedules"); artık security_intl
        # de (SADECE MEGA'da dynamic ise) buraya ekleniyor - `_predict_
        # airport_with_coupling`'in `effective_servers_by_hour` hesabı
        # bunu process bazlı okuyor.
        "dynamic_schedules": {
            PROCESS_PASSPORT_DEPARTURE: passport_result.get("departure_schedule"),
            PROCESS_PASSPORT_ARRIVAL: passport_result.get("arrival_schedule"),
            PROCESS_SECURITY_INTL: security_intl_schedule,
        },
        "dynamic_checkpoint_logs": {
            PROCESS_PASSPORT_DEPARTURE: passport_result.get("departure_checkpoint_log"),
            PROCESS_PASSPORT_ARRIVAL: passport_result.get("arrival_checkpoint_log"),
            PROCESS_SECURITY_INTL: security_intl_checkpoint_log,
        },
    }


DISPLAY_5M_PROCESSES = (
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
)


def five_minute_wait_series(
    events, window_minutes: int = WAIT_DISPLAY_BUCKET_MINUTES,
) -> list[tuple[datetime, float, float, float]]:
    """ADIM (Current-Queue Weighted Remaining Wait) - bu fonksiyon ANA
    display grafiğinin kaynağı DEĞİL ARTIK (bkz. `event_driven_display_
    series`/`_remaining_wait_at_checkpoint` - "şu an kuyrukta bekleyenlerin
    ortalama KALAN wait'i"). `five_minute_wait_series()` ESKİ "bu
    pencerede yeni gelenlerin ortalama wait'i" metriğini hesaplıyor -
    IST'te 20:17'den sonra yeni arrival kesilince değerin 213dk'dan
    aniden 0'a düşmesine (yanıltıcı "queue bitti" izlenimi) yol açtığı
    için ana grafikten kaldırıldı. Fonksiyonun KENDİSİ (doğru, test
    edilmiş, saf bir yardımcı) SQL-yan audit/validation karşılaştırması
    için KORUNDU - `window_minutes`'lık pencerelerde, o pencerede
    `arrival_time`'ı düşen event'lerin passenger-weighted ortalama
    wait'i - `(window_start, weighted_avg_wait, passenger_count,
    wait_numerator)`. Sadece VERİSİ OLAN pencereleri döndürür."""
    wait_numerator: dict[datetime, float] = {}
    passenger_count: dict[datetime, float] = {}
    for event in events:
        key = floor_to_window(event.arrival_time, window_minutes)
        wait_numerator[key] = wait_numerator.get(key, 0.0) + event.wait_minutes * event.count
        passenger_count[key] = passenger_count.get(key, 0.0) + event.count
    return sorted(
        (key, wait_numerator[key] / passenger_count[key], passenger_count[key], wait_numerator[key])
        for key in wait_numerator
        if passenger_count[key] > 0
    )


def _display_day_bounds(now: datetime, tz=None) -> tuple[datetime, datetime]:
    if tz is not None:
        return operational_day_window(tz, now)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


@dataclass(frozen=True)
class FiveMinuteWaitPoint:
    # ADIM (Current-Queue Weighted Remaining Wait) - grafik metriği
    # ARTIK "bu pencerede yeni gelenlerin ortalama wait'i" (eski
    # `passenger_weighted_arrival_window`, IST 20:17 sonrası yeni arrival
    # kesilince 213dk'dan aniden 0'a düşen artifact'e yol açmıştı) DEĞİL
    # - "ŞU CHECKPOINT ANINDA hâlâ kuyrukta bekleyen (arrival_time<=
    # checkpoint<service_start) yolcuların passenger-weighted ORTALAMA
    # KALAN bekleme süresi" (`remaining_wait = service_start-checkpoint`).
    # Yeni arrival gelmese bile, kuyrukta bekleyen biri VARSA değer
    # sıfırdan BÜYÜK kalır (yalancı ani düşüş YOK); bir yolcu `service_
    # start`'a ulaştığı anda (artık serviste/tamamlanmış) hesaba hiç
    # DAHİL EDİLMEZ - bkz. `_remaining_wait_at_checkpoint`.
    window_start: datetime
    wait_minutes: float
    passenger_count: float
    wait_numerator: float


def _remaining_wait_at_checkpoint(
    events, checkpoint_time: datetime,
) -> tuple[float, float, float]:
    """Şu ANDA (checkpoint_time) GERÇEKTEN kuyrukta bekleyen (henüz
    servise girmemiş) her event için `remaining_wait = service_start_
    time - checkpoint_time`, passenger-weighted ortalaması. Farklı bir
    hesap İCAT EDİLMİYOR - SADECE production'ın zaten ürettiği `arrival_
    time`/`service_start_time`/`count` okunuyor, FIFO/dynamic staffing/
    service-time matematiğine HİÇ dokunulmuyor. `service_start_time <=
    checkpoint_time` olan (artık serviste veya tamamlanmış) event'ler
    KASITLI OLARAK hariç tutuluyor - onlar artık "bekleyen" değil.
    Kimse beklemiyorsa (`denominator=0`) `(0.0, 0.0, 0.0)` döner."""
    numerator = 0.0
    denominator = 0.0
    for event in events:
        if event.arrival_time <= checkpoint_time < event.service_start_time:
            remaining_minutes = (event.service_start_time - checkpoint_time).total_seconds() / 60.0
            numerator += remaining_minutes * event.count
            denominator += event.count
    average = numerator / denominator if denominator > 0 else 0.0
    return average, denominator, numerator


def event_driven_display_series(
    flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    now: datetime | None = None,
    tz=None,
    display_bucket_minutes: int = WAIT_DISPLAY_BUCKET_MINUTES,
) -> dict[str, list[FiveMinuteWaitPoint]]:
    """
    CURRENT-QUEUE WEIGHTED REMAINING WAIT: her `display_bucket_minutes`
    checkpoint'inde, o anda GERÇEKTEN kuyrukta bekleyen (`arrival_time
    <= checkpoint < service_start_time`) yolcuların passenger-weighted
    ortalama KALAN bekleme süresini verir (bkz. `_remaining_wait_at_
    checkpoint`). Tam gün için (24h/`display_bucket_minutes`) SABİT
    sayıda nokta üretir; hiç kimse beklemiyorsa (queue GERÇEKTEN boşsa)
    `wait_minutes=0.0` - ama backlog varsa (yeni arrival gelmese bile)
    değer sıfıra ANİDEN düşmez, kuyruk gerçekten boşalana kadar doğal
    şekilde azalır. Saatlik `predict_airport()`/`QueuePrediction`
    semantics'ini DEĞİŞTİRMEZ (ayrı, kendi hesabı, `_bucket_weighted_
    wait` hâlâ arrival-window bazlı).
    """
    _now = now if now is not None else domain_now()
    coupling = _event_driven_queue_demand(
        flights, config, demand, window_minutes=window_minutes, now=_now
    )

    day_start, day_end = _display_day_bounds(_now, tz)
    probe_times: list[datetime] = []
    probe = day_start
    step = timedelta(minutes=display_bucket_minutes)
    while probe < day_end:
        probe_times.append(probe)
        probe += step

    series: dict[str, list[FiveMinuteWaitPoint]] = {}
    for process in DISPLAY_5M_PROCESSES:
        events = coupling["process_events"][process]
        points: list[FiveMinuteWaitPoint] = []
        for probe_time in probe_times:
            wait, pax, numerator = _remaining_wait_at_checkpoint(events, probe_time)
            points.append(FiveMinuteWaitPoint(
                window_start=probe_time, wait_minutes=wait,
                passenger_count=pax, wait_numerator=numerator,
            ))
        series[process] = points
    return series


def persist_display_series(
    session, airport_iata: str, series: dict[str, list[FiveMinuteWaitPoint]],
) -> int:
    now = datetime.now(timezone.utc)
    session.query(QueueWaitDisplay5m).filter_by(airport_iata=airport_iata).delete()
    written = 0
    for process, points in series.items():
        for point in points:
            session.add(QueueWaitDisplay5m(
                airport_iata=airport_iata,
                process=process,
                window_start=point.window_start,
                estimated_wait_minutes=point.wait_minutes,
                wait_numerator=point.wait_numerator,
                passenger_count=point.passenger_count,
                aggregation_method="current_queue_weighted_remaining_wait",
                risk=risk_from_wait(point.wait_minutes),
                calculated_at=now,
            ))
            written += 1
    session.commit()
    return written


def predict_airport(
    airport_iata: str,
    flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    baseline_fn=None,
    aircraft_match_rate: float | None = None,
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    passenger_baseline_fn=None,
    now: datetime | None = None,
) -> list[WindowPrediction]:
    predictions, _coupling = _predict_airport_with_coupling(
        airport_iata=airport_iata, flights=flights, config=config, demand=demand,
        baseline_fn=baseline_fn, aircraft_match_rate=aircraft_match_rate,
        aircraft_changes=aircraft_changes, window_minutes=window_minutes,
        passenger_baseline_fn=passenger_baseline_fn, now=now,
    )
    return predictions


def _predict_airport_with_coupling(
    airport_iata: str,
    flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    baseline_fn=None,
    aircraft_match_rate: float | None = None,
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    passenger_baseline_fn=None,
    now: datetime | None = None,
) -> tuple[list[WindowPrediction], dict]:
    """`predict_airport()`'un AYNISI - tek fark, hesaplamada zaten
    ÜRETİLMİŞ `coupling` dict'ini (ServiceEvent'ler, backlog, dynamic
    staffing schedule'ları) de döndürmesi. Bu SADECE `run_predictions()`
    içindeki audit adımının `coupling`'i İKİNCİ KEZ hesaplamak ZORUNDA
    kalmaması içindir (performans - Bölüm 31) - `predict_airport()`'un
    KENDİSİ/DÖNÜŞ TİPİ/mevcut testleri DEĞİŞMEDİ."""
    try:
        passport_capacity_rate(config)
        passport_departure_capacity_rate(config)
        passport_arrival_capacity_rate(config)
        security_capacity_rate(config)
        domestic_security_capacity_rate(config)
        international_security_capacity_rate(config)
    except ValueError as exc:
        raise ValueError(f"{airport_iata}: geçersiz queue config - {exc}") from exc

    if aircraft_match_rate is None:
        aircraft_match_rate = flight_match_rate(flights)

    predictions: list[WindowPrediction] = []

    coupling = _event_driven_queue_demand(
        flights, config, demand, window_minutes=window_minutes, now=now
    )
    lane_count_overrides = {
        PROCESS_SECURITY_DOMESTIC: config.domestic_security_lane_count,
        PROCESS_SECURITY_INTL: config.international_security_lane_count,
    }
    flight_by_key = {f.flight_key: f for f in flights}
    attributed_flight_count_processes = (PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL)

    for process in PROCESSES:
        relevant = _PROCESS_FLIGHTS[process](flights)
        buckets = _bucket_flights_by_window(relevant, window_minutes)

        coupled_demand = coupling["demand_by_hour"][process]
        coupled_backlog = coupling["backlog_start_by_hour"][process]
        coupled_current = coupling["current_released_by_hour"][process]
        coupled_wait = coupling["event_wait_by_hour"][process]
        starts = sorted(set(buckets) | set(coupled_demand) | set(coupled_backlog))

        risk_backlog_by_hour = (
            _event_derived_backlog_by_hour(coupling["process_events"][process], starts)
            if process in _EVENT_DRIVEN_WAIT_PROCESSES else {}
        )

        effective_servers_by_hour: dict = {}
        schedule = coupling.get("dynamic_schedules", {}).get(process)
        if schedule is not None:
            effective_servers_by_hour = effective_capacity_by_hour(
                schedule, starts, window_minutes
            )

        contributing_keys_by_hour = coupling["contributing_flight_keys_by_hour"][process]

        for start in starts:
            window_all = buckets.get(start, [])
            if process in attributed_flight_count_processes:
                window_flights = [
                    flight_by_key[key]
                    for key in contributing_keys_by_hour.get(start, frozenset())
                    if key in flight_by_key
                ]
            else:
                window_flights = [
                    f for f in window_all if f.status not in EXCLUDED_STATUSES
                ]
            baseline = baseline_fn(process, start) if baseline_fn else None
            passenger_baseline = (
                passenger_baseline_fn(process, start)
                if passenger_baseline_fn else None
            )
            predictions.append(_predict_window_core(
                airport_iata=airport_iata,
                process=process,
                window_start=start,
                window_flights=window_flights,
                window_all=window_all,
                config=config,
                demand=demand,
                historical_baseline=baseline,
                aircraft_match_rate=aircraft_match_rate,
                backlog_start=coupled_backlog.get(start, 0.0),
                aircraft_changes=aircraft_changes,
                window_minutes=window_minutes,
                historical_passenger_baseline=passenger_baseline,
                now=now,
                demand_override=coupled_demand.get(start, 0.0),
                current_arrived_override=coupled_current.get(start, 0.0),
                lane_count_override=lane_count_overrides.get(process),
                event_driven_wait_override=(
                    coupled_wait.get(start)
                    if process in _EVENT_DRIVEN_WAIT_PROCESSES else None
                ),
                risk_backlog_start=risk_backlog_by_hour.get(start, 0.0),
                server_count_override=effective_servers_by_hour.get(start),
            ))

    return predictions, coupling


def flight_match_rate(flights) -> float:
    if not flights:
        return 0.0
    matched = sum(
        1 for f in flights if getattr(f, "aircraft_icao", None) is not None
    )
    return matched / len(flights)



def airport_codes(session) -> list[str]:
    rows = session.execute(
        select(Flight.airport_iata).distinct().order_by(Flight.airport_iata)
    ).scalars().all()
    return [code for code in rows if code]


def flights_of_airport(
    session, airport_iata: str, now: datetime | None = None,
    horizon_hours: int = USAGE_HORIZON_HOURS,
) -> list[Flight]:
    rows = list(session.execute(
        select(Flight).where(Flight.airport_iata == airport_iata)
    ).scalars().all())

    if now is None:
        return rows

    cutoff = now - timedelta(hours=horizon_hours)
    result = []
    for flight in rows:
        canonical = canonical_flight_time(flight)
        if canonical is None or canonical >= cutoff:
            result.append(flight)
    return result


def _airport_timezones(session, airport_codes) -> dict[str, str | None]:
    codes = list(airport_codes)
    if not codes:
        return {}
    rows = session.execute(
        select(Airport.iata_code, Airport.timezone).where(
            Airport.iata_code.in_(codes)
        )
    ).all()
    return dict(rows)


def _load_historical_flight_count_cache(
    session, airport_iata: str
) -> dict[tuple[str, int, int], HistoricalFlightCount]:
    rows = session.execute(
        select(HistoricalFlightCount).where(HistoricalFlightCount.airport_iata == airport_iata)
    ).scalars().all()
    return {(r.process, r.hour_of_day, r.day_of_week): r for r in rows}


def _db_baseline_fn(session, airport_iata: str, cache: dict | None = None):
    if cache is None:
        cache = _load_historical_flight_count_cache(session, airport_iata)

    def lookup(process: str, window_start: datetime) -> float | None:
        row = cache.get((process, window_start.hour, window_start.weekday()))
        if row is None or row.sample_size <= 0:
            return None
        return row.average_flight_count
    return lookup


def _db_passenger_baseline_fn(session, airport_iata: str, cache: dict | None = None):
    if cache is None:
        cache = _load_historical_flight_count_cache(session, airport_iata)

    def lookup(process: str, window_start: datetime) -> float | None:
        row = cache.get((process, window_start.hour, window_start.weekday()))
        if row is None or row.passenger_sample_size <= 0:
            return None
        return row.average_expected_passengers
    return lookup


PREDICTION_PERSIST_CHUNK_SIZE = 500

_PREDICTION_COMPARE_COLUMNS = (
    "window_end",
    "operational_date",
    "flight_count",
    "expected_passengers",
    "baseline_ratio",
    "flight_ratio",
    "passenger_ratio",
    "utilization",
    "estimated_wait_minutes",
    "risk",
    "confidence",
)


def _prediction_as_candidate_values(prediction: WindowPrediction) -> dict:
    return {
        "window_end": prediction.window_end,
        "operational_date": prediction.operational_date,
        "flight_count": prediction.flight_count,
        "expected_passengers": prediction.expected_passengers,
        "baseline_ratio": prediction.baseline_ratio,
        "flight_ratio": prediction.flight_ratio,
        "passenger_ratio": prediction.passenger_ratio,
        "utilization": prediction.utilization,
        "estimated_wait_minutes": prediction.estimated_wait_minutes,
        "risk": prediction.risk,
        "reasons": prediction.reasons_json(),
        "confidence": prediction.confidence,
    }


def _prediction_differs(existing: QueuePrediction, prediction: WindowPrediction) -> bool:
    for column in _PREDICTION_COMPARE_COLUMNS:
        if getattr(existing, column) != getattr(prediction, column):
            return True
    if existing.reasons != prediction.reasons_json():
        return True
    return False


def _apply_prediction_fields(existing: QueuePrediction, prediction: WindowPrediction, now: datetime) -> None:
    values = _prediction_as_candidate_values(prediction)
    for column, value in values.items():
        setattr(existing, column, value)
    existing.calculated_at = now


def persist_predictions(session, predictions: list[WindowPrediction]) -> dict:
    now = datetime.now(timezone.utc)
    inserted = 0
    updated = 0

    if not predictions:
        session.commit()
        return {"inserted": 0, "updated": 0}

    keys = [
        (p.airport_iata, p.process, p.window_start) for p in predictions
    ]
    existing_by_key: dict[tuple[str, str, datetime], QueuePrediction] = {}
    key_tuple = tuple_(
        QueuePrediction.airport_iata, QueuePrediction.process, QueuePrediction.window_start
    )
    for start in range(0, len(keys), PREDICTION_PERSIST_CHUNK_SIZE):
        chunk_keys = keys[start:start + PREDICTION_PERSIST_CHUNK_SIZE]
        rows = session.execute(
            select(QueuePrediction).where(key_tuple.in_(chunk_keys))
        ).scalars().all()
        for row in rows:
            existing_by_key[(row.airport_iata, row.process, row.window_start)] = row

    for prediction in predictions:
        key = (prediction.airport_iata, prediction.process, prediction.window_start)
        existing = existing_by_key.get(key)

        if existing is None:
            values = _prediction_as_candidate_values(prediction)
            new_row = QueuePrediction(
                airport_iata=prediction.airport_iata,
                process=prediction.process,
                window_start=prediction.window_start,
                calculated_at=now,
                **values,
            )
            session.add(new_row)
            existing_by_key[key] = new_row
            inserted += 1
            continue

        updated += 1
        if _prediction_differs(existing, prediction):
            _apply_prediction_fields(existing, prediction, now)

    session.commit()
    return {"inserted": inserted, "updated": updated}


def prune_stale_predictions(
    session,
    airport_iata: str,
    keep: set[tuple[str, datetime]],
    operational_date_value: date | None = None,
) -> int:
    query = select(QueuePrediction).where(QueuePrediction.airport_iata == airport_iata)
    if operational_date_value is not None:
        query = query.where(QueuePrediction.operational_date == operational_date_value)
    rows = session.execute(query).scalars().all()

    removed = 0
    for row in rows:
        if (row.process, row.window_start) not in keep:
            session.delete(row)
            removed += 1

    if removed:
        session.commit()
    return removed


def record_baseline_observations(
    session, predictions: list[WindowPrediction], now: datetime | None = None
) -> int:
    now = now if now is not None else domain_now()
    recorded = 0

    closed_predictions = [p for p in predictions if now >= p.window_end]
    closed_keys = [
        (p.airport_iata, p.process, p.window_start) for p in closed_predictions
    ]
    already_recorded = existing_baseline_observation_keys(session, closed_keys)

    old_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        for prediction in closed_predictions:
            key = (prediction.airport_iata, prediction.process, prediction.window_start)
            if key in already_recorded:
                continue
            record_observation(
                session,
                airport_iata=prediction.airport_iata,
                process=prediction.process,
                window_start=prediction.window_start,
                hour_of_day=prediction.window_start.hour,
                day_of_week=prediction.window_start.weekday(),
                flight_count=prediction.flight_count,
                expected_passengers=prediction.expected_passengers,
            )
            recorded += 1
    finally:
        session.expire_on_commit = old_expire_on_commit

    return recorded


def _record_calculation_audit(
    session, run_id: str, airport_iata: str, flights: list, config, demand, tz,
    predictions: list, coupling: dict, display_series: dict, calculation_date, now: datetime,
    codeshare_records_removed: int = 0,
) -> None:
    """Zaten TAMAMLANMIŞ bir hesaplamanın (predictions/coupling/display_
    series) SQL'den izlenebilir bir kopyasını audit tablolarına yazar.
    Bkz. app/queue/audit.py modül docstring'i - bu fonksiyon `predictions`/
    `coupling`'i ASLA DEĞİŞTİRMEZ (sadece okur), ve herhangi bir hata
    burada YAKALANIP loglanır - çağıranın gerçek prediction/display
    sonucunu ETKİLEMEZ (Bölüm 32 - transaction safety)."""
    from . import audit as _queue_audit

    if not _queue_audit.audit_enabled():
        return
    try:
        from .ingestion.airports_import import country_lookup

        country_by_iata = country_lookup(session)
        _queue_audit.record_resource_config_audit(session, run_id, airport_iata, config)
        _queue_audit.record_routing_and_country_audit(
            session, run_id, airport_iata, flights, demand, country_by_iata, calculation_date,
            codeshare_records_removed=codeshare_records_removed,
        )
        _queue_audit.record_flight_cohort_and_contribution_audit(
            session, run_id, airport_iata, flights, demand, tz, country_by_iata, coupling,
        )
        _queue_audit.record_service_event_audit(session, run_id, airport_iata, coupling, config, tz)
        _queue_audit.record_hourly_calculation_audit(
            session, run_id, airport_iata, config, tz, predictions, coupling, calculation_date,
        )
        _queue_audit.record_dynamic_staffing_audit(session, run_id, airport_iata, coupling, tz, config)
        for process, points in display_series.items():
            _queue_audit.record_graph_display_audit(session, run_id, airport_iata, process, points, tz, now)
        _queue_audit.record_flight_resolution_audit(
            session, run_id, airport_iata, flights, demand, country_by_iata,
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "Queue calculation audit yazılamadı (airport=%s, run_id=%s) - "
            "GERÇEK prediction/display sonucu ETKİLENMEDİ, sadece audit "
            "satırları eksik kaldı.",
            airport_iata, run_id,
        )


def run_predictions(
    session,
    resolver,
    airports: list[str] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    update_baseline: bool = True,
    now: datetime | None = None,
    apply_usage_horizon: bool = False,
    codeshare_removed_by_airport: dict[str, int] | None = None,
) -> dict:
    from .ingestion.refresh import aircraft_changes_for_airport
    from . import audit as _queue_audit

    resolved_now = now if now is not None else domain_now()
    run_id = _queue_audit.new_run_id()

    codes = airports if airports is not None else airport_codes(session)
    configs = get_configs(session, codes)
    timezones = _airport_timezones(session, codes)

    total: list[WindowPrediction] = []
    per_airport: dict[str, int] = {}
    failed_airports: list[str] = []
    timezone_missing_airports: list[str] = []
    pruned = 0

    for code in codes:
        try:
            all_flights = flights_of_airport(
                session, code, now=resolved_now if apply_usage_horizon else None,
            )
            if not all_flights:
                per_airport[code] = 0
                continue

            tz = resolve_airport_timezone(timezones.get(code))
            airport_operational_date: date | None = None
            if tz is not None:
                flights = filter_flights_for_operational_day(
                    all_flights, tz, resolved_now
                )
                airport_operational_date = operational_date(tz, resolved_now)
                logger.info(
                    "operational-day filtresi uygulandı (airport=%s, "
                    "local_date=%s, %d/%d uçuş seçildi)",
                    code, airport_operational_date,
                    len(flights), len(all_flights),
                )
            else:
                flights = all_flights
                timezone_missing_airports.append(code)
                logger.warning(
                    "airport=%s için güvenilir timezone kaynağı YOK "
                    "(Airport.timezone boş veya zoneinfo'da tanınmıyor) - "
                    "operational-day filtresi UYGULANMADI, tüm geçmiş "
                    "flight'lar kullanıldı (eski davranış, limitation).",
                    code,
                )

            if not flights:
                per_airport[code] = 0
                continue

            historical_cache = _load_historical_flight_count_cache(session, code)
            demand_calculator = DemandCalculator(resolver)
            predictions, coupling = _predict_airport_with_coupling(
                airport_iata=code,
                flights=flights,
                config=configs[code],
                demand=demand_calculator,
                baseline_fn=_db_baseline_fn(session, code, cache=historical_cache),
                passenger_baseline_fn=_db_passenger_baseline_fn(session, code, cache=historical_cache),
                aircraft_changes=aircraft_changes_for_airport(session, code),
                window_minutes=window_minutes,
                now=resolved_now,
            )
            display_series = event_driven_display_series(
                flights, configs[code], demand_calculator,
                window_minutes=window_minutes, now=resolved_now, tz=tz,
            )
            persist_display_series(session, code, display_series)
            for p in predictions:
                p.operational_date = airport_operational_date
            per_airport[code] = len(predictions)
            total.extend(predictions)

            pruned += prune_stale_predictions(
                session,
                code,
                {(p.process, p.window_start) for p in predictions},
                operational_date_value=airport_operational_date,
            )

            _record_calculation_audit(
                session, run_id=run_id, airport_iata=code, flights=flights,
                config=configs[code], demand=demand_calculator, tz=tz,
                predictions=predictions, coupling=coupling, display_series=display_series,
                calculation_date=airport_operational_date, now=resolved_now,
                codeshare_records_removed=(
                    codeshare_removed_by_airport.get(code, 0)
                    if codeshare_removed_by_airport else 0
                ),
            )
        except Exception:
            session.rollback()
            failed_airports.append(code)
            logger.exception(
                "Havalimanı için tahmin üretilemedi, atlanıyor (airport=%s)",
                code,
            )
            continue

    written = persist_predictions(session, total)

    if update_baseline:
        record_baseline_observations(session, total, now=resolved_now)

    return {
        "run_id": run_id,
        "airports": per_airport,
        "predictions": len(total),
        "pruned": pruned,
        "failed_airports": failed_airports,
        "timezone_missing_airports": timezone_missing_airports,
        **written,
    }
