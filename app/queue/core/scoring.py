
from typing import Callable, Sequence

from .erlang import erlang_c_wait_time
from ..constants import (
    AIRCRAFT_MATCH_RATE_THRESHOLD,
    CONFIDENCE_FLOOR,
    CONFIDENCE_PENALTY_BOARDING_BUFFER,
    CONFIDENCE_PENALTY_DEFAULT_CONFIG,
    CONFIDENCE_PENALTY_LOAD_FACTOR,
    CONFIDENCE_PENALTY_LOW_MATCH_RATE,
    CONFIDENCE_PENALTY_NO_BASELINE,
    CONFIDENCE_PENALTY_NULL_AIRCRAFT,
    DEMAND_WINDOW_MINUTES,
    NO_BASELINE_MESSAGE,
    PASSPORT_OVERLOAD_MESSAGE,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    SECURITY_FLIGHT_RATIO_WEIGHT,
    SECURITY_PASSENGER_RATIO_WEIGHT,
    SECURITY_RATIO_HIGH,
    SECURITY_RATIO_LOW,
    SECURITY_RATIO_MEDIUM,
    WAIT_RISK_HIGH_MINUTES,
    WAIT_RISK_LOW_MINUTES,
    WAIT_RISK_MEDIUM_MINUTES,
)


def risk_from_wait(wait_minutes: float | None) -> str:
    if wait_minutes is None:
        return RISK_UNKNOWN
    if wait_minutes < WAIT_RISK_LOW_MINUTES:
        return RISK_LOW
    if wait_minutes < WAIT_RISK_MEDIUM_MINUTES:
        return RISK_MEDIUM
    if wait_minutes < WAIT_RISK_HIGH_MINUTES:
        return RISK_HIGH
    return RISK_CRITICAL


def combined_security_ratio(
    flight_ratio: float, passenger_ratio: float | None
) -> float:
    if passenger_ratio is None:
        return flight_ratio

    total_weight = (
        SECURITY_FLIGHT_RATIO_WEIGHT + SECURITY_PASSENGER_RATIO_WEIGHT
    )
    if total_weight <= 0:
        return flight_ratio

    return (
        SECURITY_FLIGHT_RATIO_WEIGHT * flight_ratio
        + SECURITY_PASSENGER_RATIO_WEIGHT * passenger_ratio
    ) / total_weight


def security_density_score(
    window_flights: Sequence,
    historical_baseline: float | None,
    demand_fn: Callable[[object], int],
    historical_passenger_baseline: float | None = None,
) -> dict:
    flight_count = len(window_flights)
    demand = sum(demand_fn(f) for f in window_flights)

    if not historical_baseline:
        return {
            "flight_count": flight_count,
            "expected_passengers": demand,
            "baseline_ratio": None,
            "flight_ratio": None,
            "passenger_ratio": None,
            "risk": RISK_UNKNOWN,
            "estimated_wait_minutes": None,
            "reasons": [NO_BASELINE_MESSAGE],
        }

    flight_ratio = flight_count / historical_baseline

    passenger_ratio = (
        demand / historical_passenger_baseline
        if historical_passenger_baseline else None
    )

    ratio = combined_security_ratio(flight_ratio, passenger_ratio)

    if ratio < SECURITY_RATIO_LOW:
        risk = RISK_LOW
    elif ratio < SECURITY_RATIO_MEDIUM:
        risk = RISK_MEDIUM
    elif ratio < SECURITY_RATIO_HIGH:
        risk = RISK_HIGH
    else:
        risk = RISK_CRITICAL

    return {
        "flight_count": flight_count,
        "expected_passengers": demand,
        "baseline_ratio": round(ratio, 2),
        "flight_ratio": round(flight_ratio, 2),
        "passenger_ratio": (
            round(passenger_ratio, 2) if passenger_ratio is not None else None
        ),
        "risk": risk,
        "estimated_wait_minutes": None,
        "reasons": [],
    }


def passport_effective_service_rate(config) -> float:
    service_time = config.passport_service_time_minutes
    if service_time <= 0:
        raise ValueError(
            "passport_service_time_minutes > 0 olmalı "
            f"(alınan={service_time})"
        )
    return 1.0 / service_time


def passport_effective_server_count(config) -> int:
    counters = config.passport_counter_count
    staff_per_counter = config.passport_staff_per_counter
    if counters <= 0:
        raise ValueError(f"passport_counter_count > 0 olmalı (alınan={counters})")
    if staff_per_counter <= 0:
        raise ValueError(
            f"passport_staff_per_counter > 0 olmalı (alınan={staff_per_counter})"
        )
    return round(counters * staff_per_counter)


def passport_server_count(config, pool: str) -> int:
    if pool == "departure":
        count = config.passport_departure_server_count
    elif pool == "arrival":
        count = config.passport_arrival_server_count
    else:
        raise ValueError(f'pool "departure" veya "arrival" olmalı (alınan={pool!r})')
    if count <= 0:
        raise ValueError(
            f"passport_{pool}_server_count > 0 olmalı (alınan={count})"
        )
    return count


def passport_departure_server_count(config) -> int:
    return passport_server_count(config, "departure")


def passport_arrival_server_count(config) -> int:
    return passport_server_count(config, "arrival")


def security_effective_service_rate(config) -> float:
    service_time = config.security_service_time_minutes
    if service_time <= 0:
        raise ValueError(
            "security_service_time_minutes > 0 olmalı "
            f"(alınan={service_time})"
        )
    return 1.0 / service_time


def queue_capacity_rate(server_count: int, service_time_minutes: float) -> float:
    if server_count <= 0:
        raise ValueError(f"server_count > 0 olmalı (alınan={server_count})")
    if service_time_minutes <= 0:
        raise ValueError(
            "service_time_minutes > 0 olmalı "
            f"(alınan={service_time_minutes})"
        )
    return server_count * (1.0 / service_time_minutes)


def passport_capacity_rate(config) -> float:
    return queue_capacity_rate(
        passport_effective_server_count(config),
        config.passport_service_time_minutes,
    )


def passport_departure_capacity_rate(config) -> float:
    return queue_capacity_rate(
        passport_departure_server_count(config),
        config.passport_service_time_minutes,
    )


def passport_arrival_capacity_rate(config) -> float:
    return queue_capacity_rate(
        passport_arrival_server_count(config),
        config.passport_service_time_minutes,
    )


def security_capacity_rate(config) -> float:
    return queue_capacity_rate(
        config.security_lane_count,
        config.security_service_time_minutes,
    )


def domestic_security_capacity_rate(config) -> float:
    return queue_capacity_rate(
        config.domestic_security_lane_count,
        config.security_service_time_minutes,
    )


def international_security_capacity_rate(config) -> float:
    return queue_capacity_rate(
        config.international_security_lane_count,
        config.security_service_time_minutes,
    )


def passport_staff_count_mismatch(config) -> bool:
    expected = config.passport_counter_count * config.passport_staff_per_counter
    return config.passport_staff_count != expected


def queue_capacity_model(
    window_flights: Sequence,
    demand_fn: Callable[[object], int],
    server_count: int,
    service_time_minutes: float,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    backlog_start: float = 0.0,
    current_arrived_demand: float | None = None,
    elapsed_minutes: float | None = None,
    demand_override: float | None = None,
    risk_backlog_start: float = 0.0,
) -> dict:
    demand = (
        sum(demand_fn(f) for f in window_flights)
        if demand_override is None else demand_override
    )
    lam = demand / window_minutes
    c = server_count
    mu = 1.0 / service_time_minutes if service_time_minutes > 0 else 0.0
    capacity_rate = queue_capacity_rate(c, service_time_minutes)
    rho = lam / capacity_rate

    service_capacity = capacity_rate * window_minutes
    backlog_end = max(0.0, backlog_start + demand - service_capacity)

    queue_pressure = (demand + risk_backlog_start) / service_capacity

    reasons = [PASSPORT_OVERLOAD_MESSAGE] if queue_pressure >= 1.0 else []

    if backlog_start <= 0.0 and rho < 1.0:
        wq = erlang_c_wait_time(c, lam, mu)
    elif demand <= 0.0:
        wq = backlog_start / capacity_rate
    else:
        arrived = demand if current_arrived_demand is None else current_arrived_demand
        elapsed = window_minutes if elapsed_minutes is None else elapsed_minutes
        elapsed = max(0.0, min(elapsed, window_minutes))
        served_since_window_start = capacity_rate * elapsed
        current_queue = max(0.0, backlog_start + arrived - served_since_window_start)
        wq = current_queue / capacity_rate

    risk = risk_from_wait(wq)

    return {
        "flight_count": len(window_flights),
        "expected_passengers": demand,
        "arrival_rate": round(lam, 3),
        "server_count": c,
        "service_rate": round(mu, 6),
        "capacity_rate": round(capacity_rate, 6),
        "utilization": round(rho, 3),
        "queue_pressure": round(queue_pressure, 3),
        "estimated_wait_minutes": round(wq, 1),
        "backlog_end": round(backlog_end, 3),
        "risk": risk,
        "reasons": reasons,
    }


def passport_queue_model(
    window_flights: Sequence,
    config,
    demand_fn: Callable[[object], int],
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    backlog_start: float = 0.0,
    current_arrived_demand: float | None = None,
    elapsed_minutes: float | None = None,
    demand_override: float | None = None,
    pool: str | None = None,
    risk_backlog_start: float = 0.0,
    server_count_override: float | None = None,
) -> dict:
    server_count = (
        round(server_count_override) if server_count_override is not None
        else passport_effective_server_count(config) if pool is None
        else passport_server_count(config, pool)
    )
    return queue_capacity_model(
        window_flights=window_flights,
        demand_fn=demand_fn,
        server_count=server_count,
        service_time_minutes=config.passport_service_time_minutes,
        window_minutes=window_minutes,
        backlog_start=backlog_start,
        current_arrived_demand=current_arrived_demand,
        elapsed_minutes=elapsed_minutes,
        demand_override=demand_override,
        risk_backlog_start=risk_backlog_start,
    )


def security_queue_model(
    window_flights: Sequence,
    config,
    demand_fn: Callable[[object], int],
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    backlog_start: float = 0.0,
    current_arrived_demand: float | None = None,
    elapsed_minutes: float | None = None,
    demand_override: float | None = None,
    lane_count_override: int | None = None,
    risk_backlog_start: float = 0.0,
) -> dict:
    return queue_capacity_model(
        window_flights=window_flights,
        demand_fn=demand_fn,
        server_count=(
            config.security_lane_count
            if lane_count_override is None else lane_count_override
        ),
        service_time_minutes=config.security_service_time_minutes,
        window_minutes=window_minutes,
        backlog_start=backlog_start,
        current_arrived_demand=current_arrived_demand,
        elapsed_minutes=elapsed_minutes,
        demand_override=demand_override,
        risk_backlog_start=risk_backlog_start,
    )


def confidence_score(
    window_flights: Sequence,
    historical_baseline_available: bool,
    aircraft_match_rate: float,
    config_is_default: bool,
) -> float:
    score = 1.0

    score -= CONFIDENCE_PENALTY_LOAD_FACTOR
    score -= CONFIDENCE_PENALTY_BOARDING_BUFFER

    if aircraft_match_rate < AIRCRAFT_MATCH_RATE_THRESHOLD:
        score -= CONFIDENCE_PENALTY_LOW_MATCH_RATE

    if any(getattr(f, "aircraft_icao", None) is None for f in window_flights):
        score -= CONFIDENCE_PENALTY_NULL_AIRCRAFT

    if not historical_baseline_available:
        score -= CONFIDENCE_PENALTY_NO_BASELINE

    if config_is_default:
        score -= CONFIDENCE_PENALTY_DEFAULT_CONFIG

    return max(round(score, 2), CONFIDENCE_FLOOR)
