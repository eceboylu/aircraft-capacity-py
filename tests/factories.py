"""
Test yardımcıları - mock uçuş ve mock kapasite çözümleyici.

Gerçek veritabanına ihtiyaç duymadan tüm hesap katmanını
sürmek için kullanılır.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)

BASE_DAY = datetime(2026, 9, 14, 0, 0)


def at(hour: int, minute: int = 0) -> datetime:
    return BASE_DAY + timedelta(hours=hour, minutes=minute)


@dataclass
class MockFlight:
    """app.queue.models.Flight ile aynı alan adlarını taşır."""

    flight_key: str = "XX_001_2026-09-14"
    airport_iata: str = "AAA"
    direction: str = DIRECTION_DEPARTURE
    location: str = LOCATION_DOMESTIC
    airline_iata: str | None = "XX"
    flight_number: str | None = "001"
    flight_iata: str | None = "XX001"
    aircraft_icao: str | None = "A320"
    aircraft_match_found: bool = True
    dep_iata: str | None = "AAA"
    arr_iata: str | None = "BBB"
    dep_scheduled_utc: datetime | None = None
    dep_estimated_utc: datetime | None = None
    dep_actual_utc: datetime | None = None
    arr_scheduled_utc: datetime | None = None
    arr_estimated_utc: datetime | None = None
    arr_actual_utc: datetime | None = None
    dep_terminal: str | None = None
    dep_gate: str | None = None
    arr_terminal: str | None = None
    arr_gate: str | None = None
    status: str = "scheduled"


@dataclass
class MockCapacityResult:
    icao: str | None = "A320"
    airline: str | None = None
    capacity: int = 180
    source: str = "verified_dataset"
    confidence: str = "high"
    counts_toward_passenger_total: bool = True


class MockCapacityResolver:
    """
    Madde 1'in AircraftCapacityService.resolve arayüzünü taklit eder.

    capacities : icao -> koltuk sayısı
    excluded   : counts_toward_passenger_total=False dönecek icao'lar
                 (genel havacılık)
    """

    DEFAULT_CAPACITIES = {
        "A320": 180,
        "A321": 220,
        "B738": 189,
        "B77W": 350,
        "B772": 300,
        "A388": 500,
        "A359": 320,
        "E190": 100,
    }

    def __init__(self, capacities=None, excluded=(), default_capacity=180):
        self.capacities = dict(self.DEFAULT_CAPACITIES)
        if capacities:
            self.capacities.update(capacities)
        self.excluded = set(excluded)
        self.default_capacity = default_capacity
        self.calls: list[tuple] = []

    def resolve(self, icao_code, airline_iata=None) -> MockCapacityResult:
        self.calls.append((icao_code, airline_iata))
        code = (icao_code or "").upper()

        if code in self.excluded:
            return MockCapacityResult(
                icao=code,
                airline=airline_iata,
                capacity=0,
                source="general_aviation_excluded",
                confidence="high",
                counts_toward_passenger_total=False,
            )

        if code in self.capacities:
            return MockCapacityResult(
                icao=code,
                airline=airline_iata,
                capacity=self.capacities[code],
                source="verified_dataset",
                confidence="high",
                counts_toward_passenger_total=True,
            )

        return MockCapacityResult(
            icao=code,
            airline=airline_iata,
            capacity=self.default_capacity,
            source="unknown_default",
            confidence="low",
            counts_toward_passenger_total=True,
        )


def departure(
    hour,
    minute=0,
    *,
    airport="AAA",
    location=LOCATION_DOMESTIC,
    aircraft="A320",
    duration_minutes=90,
    delay=0,
    status="scheduled",
    key=None,
    airline="XX",
    number="001",
) -> MockFlight:
    """Kalkış uçuşu üretir. delay > 0 ise actual saat kaydırılır."""
    scheduled = at(hour, minute)
    actual = scheduled + timedelta(minutes=delay) if delay else None
    return MockFlight(
        flight_key=key or f"{airline}_{number}_{hour:02d}{minute:02d}",
        airport_iata=airport,
        direction=DIRECTION_DEPARTURE,
        location=location,
        airline_iata=airline,
        flight_number=number,
        flight_iata=f"{airline}{number}",
        aircraft_icao=aircraft,
        aircraft_match_found=aircraft is not None,
        dep_iata=airport,
        arr_iata="ZZZ",
        dep_scheduled_utc=scheduled,
        dep_actual_utc=actual,
        arr_scheduled_utc=scheduled + timedelta(minutes=duration_minutes),
        status=status,
    )


def arrival(
    hour,
    minute=0,
    *,
    airport="AAA",
    location=LOCATION_INTERNATIONAL,
    aircraft="A320",
    duration_minutes=90,
    delay=0,
    status="scheduled",
    key=None,
    airline="YY",
    number="002",
) -> MockFlight:
    """Varış uçuşu üretir. delay > 0 ise actual saat kaydırılır."""
    scheduled = at(hour, minute)
    actual = scheduled + timedelta(minutes=delay) if delay else None
    return MockFlight(
        flight_key=key or f"{airline}_{number}_{hour:02d}{minute:02d}",
        airport_iata=airport,
        direction=DIRECTION_ARRIVAL,
        location=location,
        airline_iata=airline,
        flight_number=number,
        flight_iata=f"{airline}{number}",
        aircraft_icao=aircraft,
        aircraft_match_found=aircraft is not None,
        dep_iata="ZZZ",
        arr_iata=airport,
        dep_scheduled_utc=scheduled - timedelta(minutes=duration_minutes),
        arr_scheduled_utc=scheduled,
        arr_actual_utc=actual,
        status=status,
    )
