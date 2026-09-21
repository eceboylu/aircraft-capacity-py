"""Generate an IST-only, AirLabs-shaped synthetic fixture for 2026-09-14.

Writes only inside this test-fixture directory. The schedule is derived for
queue/replay testing; it is not a published IST timetable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


OUT_DIR = Path(__file__).resolve().parent
DAY = datetime(2026, 9, 14)
IST_TZ = ZoneInfo("Europe/Istanbul")
UTC = ZoneInfo("UTC")

# Broad banks, deliberately not a single-hour spike.
DEP_HOURLY = (
    20, 24, 22, 20, 18, 32, 36, 40, 38, 42, 44, 43,
    41, 38, 36, 34, 38, 44, 45, 43, 40, 38, 32, 30,
)
ARR_HOURLY = (
    22, 24, 21, 20, 18, 28, 34, 39, 42, 40, 38, 44,
    48, 50, 47, 40, 36, 43, 46, 45, 40, 35, 30, 26,
)

DOMESTIC_ROUTES = (
    ("ESB", 75), ("ADB", 70), ("AYT", 80), ("TZX", 105),
    ("GZT", 100), ("ADA", 95), ("DLM", 80), ("BJV", 75),
    ("VAN", 120), ("DIY", 115), ("ASR", 85), ("ERZ", 105),
)
REGIONAL_ROUTES = (
    ("FRA", 190), ("LHR", 240), ("CDG", 220), ("AMS", 215),
    ("MUC", 170), ("VIE", 145), ("ZRH", 180), ("FCO", 165),
    ("ATH", 90), ("SOF", 80), ("BEG", 100), ("OTP", 85),
    ("DOH", 245), ("DXB", 270), ("RUH", 230), ("JED", 220),
    ("CAI", 130), ("TLV", 125), ("TBS", 135), ("GYD", 165),
)
LONG_HAUL_ROUTES = (
    ("JFK", 660), ("LAX", 790), ("IAD", 680), ("ORD", 690),
    ("YYZ", 640), ("GRU", 780), ("SIN", 650), ("HND", 700),
    ("ICN", 650), ("BKK", 570), ("PEK", 600), ("JNB", 590),
    ("ADD", 330), ("NBO", 390), ("DEL", 390), ("BOM", 410),
)

DOMESTIC_AIRCRAFT = ("A320", "A321", "A21N", "B738", "B38M", "E190")
REGIONAL_AIRCRAFT = ("A320", "A321", "A21N", "B738", "B38M", "A333")
LONG_HAUL_AIRCRAFT = ("A333", "A359", "B77W", "B789", "B788")
DOMESTIC_AIRLINES = ("TK", "PC", "VF", "XQ")
INTERNATIONAL_AIRLINES = ("TK", "LH", "BA", "AF", "KL", "EK", "QR")


def _utc(local_naive: datetime) -> datetime:
    return local_naive.replace(tzinfo=IST_TZ).astimezone(UTC).replace(tzinfo=None)


def _fmt(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else None


def _minutes(count: int, hour: int) -> list[int]:
    return sorted((int(i * 60 / count) + (hour + i * 7) % 2) % 60 for i in range(count))


def _flags(count: int, international_count: int, hour: int) -> list[bool]:
    return [((i * international_count + hour) % count) < international_count for i in range(count)]


def _international_ratio(direction: str, hour: int) -> float:
    if hour in (23, 0, 1, 2):
        return 0.88
    if direction == "departure" and hour in range(17, 23):
        return 0.80
    if direction == "arrival" and hour in range(11, 16):
        return 0.80
    return 0.76


def _route(is_international: bool, hour: int, sequence: int) -> tuple[str, int, bool]:
    if not is_international:
        airport, duration = DOMESTIC_ROUTES[(sequence + hour) % len(DOMESTIC_ROUTES)]
        return airport, duration, False
    use_long_haul = hour in (23, 0, 1, 2, 3) and sequence % 3 != 0
    routes = LONG_HAUL_ROUTES if use_long_haul else REGIONAL_ROUTES
    airport, duration = routes[(sequence * 5 + hour) % len(routes)]
    return airport, duration, use_long_haul


def _status(direction: str, local_time: datetime, sequence: int) -> tuple[str, int]:
    # Fewer than 1% cancellations and about 6% delayed flights.
    if sequence % 211 == 0:
        return "cancelled", 0
    if sequence % 17 == 0:
        return "delayed", 15 + (sequence % 3) * 10
    replay_local_now = DAY.replace(hour=15)
    if direction == "arrival" and local_time < replay_local_now - timedelta(minutes=30):
        return "landed", sequence % 7
    if replay_local_now - timedelta(minutes=30) <= local_time < replay_local_now + timedelta(minutes=30):
        return "active", sequence % 9
    if direction == "departure" and local_time < replay_local_now - timedelta(hours=3):
        return "landed", sequence % 7
    if direction == "departure" and local_time < replay_local_now - timedelta(minutes=30):
        return "active", sequence % 7
    return "scheduled", 0


def _record(direction: str, local_time: datetime, sequence: int, is_international: bool) -> dict:
    partner, duration_minutes, long_haul = _route(is_international, local_time.hour, sequence)
    airline_pool = INTERNATIONAL_AIRLINES if is_international else DOMESTIC_AIRLINES
    airline = airline_pool[(sequence + local_time.hour) % len(airline_pool)]
    aircraft_pool = LONG_HAUL_AIRCRAFT if long_haul else REGIONAL_AIRCRAFT if is_international else DOMESTIC_AIRCRAFT
    aircraft = aircraft_pool[(sequence * 3 + local_time.hour) % len(aircraft_pool)]
    flight_number = 1000 + sequence
    status, delay_minutes = _status(direction, local_time, sequence)

    if direction == "departure":
        dep_scheduled = _utc(local_time)
        arr_scheduled = dep_scheduled + timedelta(minutes=duration_minutes)
        dep_actual = dep_scheduled + timedelta(minutes=delay_minutes) if status in ("active", "landed") else None
        dep_estimated = dep_scheduled + timedelta(minutes=delay_minutes) if delay_minutes else dep_scheduled
        arr_actual = None
        arr_estimated = arr_scheduled + timedelta(minutes=delay_minutes) if delay_minutes else arr_scheduled
        dep_iata, arr_iata = "IST", partner
    else:
        arr_scheduled = _utc(local_time)
        dep_scheduled = arr_scheduled - timedelta(minutes=duration_minutes)
        arr_actual = arr_scheduled + timedelta(minutes=delay_minutes) if status == "landed" else None
        arr_estimated = arr_scheduled + timedelta(minutes=delay_minutes) if delay_minutes else arr_scheduled
        dep_actual = dep_scheduled + timedelta(minutes=delay_minutes) if status in ("active", "landed") else None
        dep_estimated = dep_scheduled + timedelta(minutes=delay_minutes) if delay_minutes else dep_scheduled
        dep_iata, arr_iata = partner, "IST"

    if status == "cancelled":
        dep_actual = None
        arr_actual = None

    return {
        "airline_iata": airline,
        "flight_iata": f"{airline}{flight_number}",
        "flight_number": str(flight_number),
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "dep_terminal": "1" if dep_iata == "IST" else None,
        "dep_gate": f"{chr(65 + sequence % 5)}{1 + sequence % 20}" if dep_iata == "IST" else None,
        "arr_terminal": "1" if arr_iata == "IST" else None,
        "arr_gate": f"{chr(65 + sequence % 5)}{1 + sequence % 20}" if arr_iata == "IST" else None,
        "arr_baggage": str(1 + sequence % 12) if arr_iata == "IST" else None,
        "dep_time": _fmt(dep_scheduled),
        "dep_time_utc": _fmt(dep_scheduled),
        "dep_estimated_utc": _fmt(dep_estimated),
        "dep_actual_utc": _fmt(dep_actual),
        "arr_time": _fmt(arr_scheduled),
        "arr_time_utc": _fmt(arr_scheduled),
        "arr_estimated_utc": _fmt(arr_estimated),
        "arr_actual_utc": _fmt(arr_actual),
        "status": status,
        "location": "international" if is_international else "domestic",
        "aircraft_icao": aircraft,
        "duration": duration_minutes,
        "delayed": delay_minutes,
    }


def _build(direction: str, hourly_counts: tuple[int, ...], start_sequence: int) -> list[dict]:
    records = []
    sequence = start_sequence
    for hour, count in enumerate(hourly_counts):
        international_count = round(count * _international_ratio(direction, hour))
        flags = _flags(count, international_count, hour)
        for minute, is_international in zip(_minutes(count, hour), flags):
            records.append(_record(direction, DAY.replace(hour=hour, minute=minute), sequence, is_international))
            sequence += 1
    return records


def _write(name: str, method: str, records: list[dict]) -> None:
    payload = {
        "request": {"method": method},
        "response": records,
        "terms": "synthetic-derived-test-fixture-not-a-published-timetable",
    }
    (OUT_DIR / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    departures = _build("departure", DEP_HOURLY, 1)
    arrivals = _build("arrival", ARR_HOURLY, 5001)
    _write("Delays - Type Departures.json", "delays", departures)
    _write("Delays - Type Arrivals.json", "delays", arrivals)
    _write("flights_live.json", "flights", [])
    print(f"departures={len(departures)} arrivals={len(arrivals)}")


if __name__ == "__main__":
    main()
