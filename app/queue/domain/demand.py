
from datetime import datetime, timedelta
from typing import Sequence

from ..constants import (
    ARRIVAL_RELEASE_BUCKET_MINUTES,
    ARRIVAL_RELEASE_PROFILE,
    DEMAND_WINDOW_MINUTES,
    DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES,
    DEPARTURE_SHOW_UP_PROFILE,
    DEPARTURE_SHOWUP_BUCKET_MINUTES,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
    PASSPORT_RELEASE_BUFFER_MINUTES,
)


class DemandCalculator:

    def __init__(self, resolver):
        self._resolver = resolver
        self._cache: dict[tuple[str | None, str | None], object] = {}

    def _resolve(self, flight):
        key = (flight.aircraft_icao, flight.airline_iata)
        if key not in self._cache:
            self._cache[key] = self._resolver.resolve(
                flight.aircraft_icao, flight.airline_iata
            )
        return self._cache[key]

    def seat_capacity(self, flight) -> int:
        result = self._resolve(flight)
        if not result.counts_toward_passenger_total:
            return 0
        return result.capacity

    def capacity_of_icao(self, aircraft_icao: str | None) -> int:
        result = self._resolver.resolve(aircraft_icao, None)
        if not result.counts_toward_passenger_total:
            return 0
        return result.capacity

    def passenger_demand(self, flight) -> int:
        result = self._resolve(flight)
        if not result.counts_toward_passenger_total:
            return 0
        return result.capacity

    def capacity_result(self, flight):
        # ADIM (SQL Audit/Traceability Genişletme, Bölüm 1) - `passenger_
        # demand()`'in KENDİSİ hâlâ SADECE `int` döndürür (davranış
        # DEĞİŞMEDİ); bu salt-okunur ek accessor, AYNI `_resolve()`
        # cache'ini kullanarak (yeniden DB sorgusu/`_flag_unknown()`
        # ÇAĞIRMADAN - key zaten `passenger_demand()` ile ısınmış olur)
        # `CapacityResult.source`/`confidence`'ı audit'e taşımak içindir.
        return self._resolve(flight)


def effective_time(flight) -> datetime | None:
    if flight.direction == DIRECTION_DEPARTURE:
        base = (
            flight.dep_actual_utc
            or flight.dep_estimated_utc
            or flight.dep_scheduled_utc
        )
        if base is None:
            return None
        return base - timedelta(minutes=DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES)

    base = (
        flight.arr_actual_utc
        or flight.arr_estimated_utc
        or flight.arr_scheduled_utc
    )
    if base is None:
        return None
    return base + timedelta(minutes=PASSPORT_RELEASE_BUFFER_MINUTES)


def _departure_show_up_base(flight) -> datetime | None:
    return (
        flight.dep_actual_utc
        or flight.dep_estimated_utc
        or flight.dep_scheduled_utc
    )


def _arrival_release_base(flight) -> datetime | None:
    # `effective_time()`'daki arrival dalının AYNISI, SADECE `+ PASSPORT_
    # RELEASE_BUFFER_MINUTES` offset'i UYGULANMADAN önceki ham zaman
    # damgası - audit.py'nin "hangi zaman kaynağı (actual/estimated/
    # scheduled) kullanıldı" sorusunu, farklı bir formül İCAT ETMEDEN
    # cevaplayabilmesi için (bkz. audit.py `record_flight_resolution_audit`).
    return (
        flight.arr_actual_utc
        or flight.arr_estimated_utc
        or flight.arr_scheduled_utc
    )


def _floor_to_global_bucket(moment: datetime, bucket_minutes: int) -> datetime:
    discard_minutes = moment.minute % bucket_minutes
    return moment.replace(second=0, microsecond=0) - timedelta(minutes=discard_minutes)


def _bucketed_profile_events_detailed(
    segments: list[tuple[datetime, datetime, float]],
    bucket_minutes: int,
    total_demand: float,
) -> list[tuple[datetime, float, int]]:
    """`_bucketed_profile_events()` ile AYNI matematik - tek fark, her
    çıktı satırının HANGİ profil segmentinden (index) geldiğini de
    taşıması. Bu SADECE `app/queue/audit.py`'nin (read-only, production
    hesabını ASLA etkilemeyen) cohort attribution'ı için kullanılır -
    `departure_show_up_events()`/`arrival_passenger_release_events()`
    hâlâ eski 2-tuple sözleşmesini döndürüyor, DEĞİŞMEDİ."""
    bucket_delta = timedelta(minutes=bucket_minutes)

    raw: list[tuple[datetime, float, int]] = []
    for segment_index, (profile_start, profile_end, fraction) in enumerate(segments):
        profile_duration_minutes = (profile_end - profile_start).total_seconds() / 60.0
        if profile_duration_minutes <= 0 or fraction <= 0:
            continue

        bucket_start = _floor_to_global_bucket(profile_start, bucket_minutes)
        while bucket_start < profile_end:
            bucket_end = bucket_start + bucket_delta
            overlap_start = max(profile_start, bucket_start)
            overlap_end = min(profile_end, bucket_end)
            overlap_minutes = (overlap_end - overlap_start).total_seconds() / 60.0
            if overlap_minutes > 0:
                contribution = total_demand * fraction * (overlap_minutes / profile_duration_minutes)
                event_time = max(profile_start, bucket_start)
                raw.append((event_time, contribution, segment_index))
            bucket_start = bucket_end

    events: list[tuple[datetime, float, int]] = []
    cumulative_target = 0.0
    cumulative_rounded = 0
    for event_time, contribution, segment_index in raw:
        cumulative_target += contribution
        new_cumulative_rounded = round(cumulative_target)
        count = new_cumulative_rounded - cumulative_rounded
        cumulative_rounded = new_cumulative_rounded
        if count <= 0:
            continue
        events.append((event_time, float(count), segment_index))
    return events


def _bucketed_profile_events(
    segments: list[tuple[datetime, datetime, float]],
    bucket_minutes: int,
    total_demand: float,
) -> list[tuple[datetime, float]]:
    return [
        (t, c) for t, c, _segment_index in
        _bucketed_profile_events_detailed(segments, bucket_minutes, total_demand)
    ]


def _departure_show_up_segments(flight) -> tuple[datetime, list[tuple[datetime, datetime, float]]] | tuple[None, None]:
    base = _departure_show_up_base(flight)
    if base is None:
        return None, None
    segments = [
        (base - timedelta(minutes=start), base - timedelta(minutes=end), fraction)
        for start, end, fraction in DEPARTURE_SHOW_UP_PROFILE
    ]
    return base, segments


def departure_show_up_events(flight, total_demand: float) -> list[tuple[datetime, float]]:
    base, segments = _departure_show_up_segments(flight)
    if base is None or total_demand <= 0:
        return []
    return _bucketed_profile_events(segments, DEPARTURE_SHOWUP_BUCKET_MINUTES, total_demand)


def departure_show_up_events_detailed(
    flight, total_demand: float,
) -> list[tuple[datetime, float, tuple[int, int, float]]]:
    """Audit-only: `departure_show_up_events()` ile AYNI sayıları üretir,
    ama her cohort'un HANGİ `DEPARTURE_SHOW_UP_PROFILE` segmentinden
    (minutes_before_start, minutes_before_end, fraction) geldiğini de
    döndürür."""
    base, segments = _departure_show_up_segments(flight)
    if base is None or total_demand <= 0:
        return []
    detailed = _bucketed_profile_events_detailed(segments, DEPARTURE_SHOWUP_BUCKET_MINUTES, total_demand)
    return [
        (t, c, DEPARTURE_SHOW_UP_PROFILE[idx]) for t, c, idx in detailed
    ]


def _arrival_release_base(flight) -> datetime | None:
    return (
        flight.arr_actual_utc
        or flight.arr_estimated_utc
        or flight.arr_scheduled_utc
    )


def is_arrival_landing_confirmed(flight) -> bool:
    # ADIM (Arrival Landing Confirmation) - kullanıcı talebi: "status='active'
    # + arr_actual_utc=NULL asla realized sayılmamalı". Tek trusted sinyal
    # `arr_actual_utc IS NOT NULL` - `status` alanı (active/landed/scheduled)
    # KASITLI OLARAK okunmuyor (provider'ın status string'i, actual
    # timestamp'i doğrulanmış GERÇEK bir iniş garantisi vermiyor - bkz.
    # gerçek DB örneği: status='landed' + arr_actual_utc=NULL). Bu, aynı
    # zamanda `status='landed' + arr_actual_utc=NULL` edge-case'inin
    # SEÇİLEN güvenli politikasıdır: actual olmadan "realized" denemez,
    # bu flight forecast kalır (mevcut estimated/scheduled bazlı release
    # timing'i KORUNUR, sadece "current/realized queue" sayılmaz).
    return flight.arr_actual_utc is not None


def _arrival_release_segments(flight) -> tuple[datetime, list[tuple[datetime, datetime, float]]] | tuple[None, None]:
    base = _arrival_release_base(flight)
    if base is None:
        return None, None
    segments = [
        (base + timedelta(minutes=start), base + timedelta(minutes=end), fraction)
        for start, end, fraction in ARRIVAL_RELEASE_PROFILE
    ]
    return base, segments


def arrival_passenger_release_events(flight, total_demand: float) -> list[tuple[datetime, float]]:
    base, segments = _arrival_release_segments(flight)
    if base is None or total_demand <= 0:
        return []
    return _bucketed_profile_events(segments, ARRIVAL_RELEASE_BUCKET_MINUTES, total_demand)


def arrival_passenger_release_events_detailed(
    flight, total_demand: float,
) -> list[tuple[datetime, float, tuple[int, int, float]]]:
    """Audit-only: `arrival_passenger_release_events()` ile AYNI sayılar,
    her cohort'un HANGİ `ARRIVAL_RELEASE_PROFILE` segmentinden geldiği
    bilgisiyle birlikte."""
    base, segments = _arrival_release_segments(flight)
    if base is None or total_demand <= 0:
        return []
    detailed = _bucketed_profile_events_detailed(segments, ARRIVAL_RELEASE_BUCKET_MINUTES, total_demand)
    return [
        (t, c, ARRIVAL_RELEASE_PROFILE[idx]) for t, c, idx in detailed
    ]


def flights_in_window(
    all_flights: Sequence,
    window_start: datetime,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    include_excluded: bool = False,
) -> list:
    window_end = window_start + timedelta(minutes=window_minutes)
    selected = []
    for flight in all_flights:
        if not include_excluded and flight.status in EXCLUDED_STATUSES:
            continue
        moment = effective_time(flight)
        if moment is None:
            continue
        if window_start <= moment < window_end:
            selected.append(flight)
    return selected
