"""Ingest the IST fixture through the existing pipeline and write a report."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[1]
DB_PATH = FIXTURE_DIR / "ist_realistic.sqlite"
REPORT_PATH = FIXTURE_DIR / "analysis_report.json"
NOW_UTC = datetime(2026, 9, 14, 12, 0)
IST_TZ = ZoneInfo("Europe/Istanbul")
UTC = ZoneInfo("UTC")

sys.path.insert(0, str(REPO_ROOT))

from app.queue.constants import (  # noqa: E402
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)
from tests.date_shift_replay.run_date_shift_replay import replay  # noqa: E402


def _local_hour(flight) -> int:
    value = (
        flight.dep_scheduled_utc
        if flight.direction == DIRECTION_DEPARTURE
        else flight.arr_scheduled_utc
    )
    return value.replace(tzinfo=UTC).astimezone(IST_TZ).hour


def _peak(rows: list[dict]) -> dict:
    demand_row = max(rows, key=lambda row: row.get("expected_passengers") or 0)
    wait_row = max(rows, key=lambda row: row.get("estimated_wait_minutes") or 0)
    return {
        "peak_demand": demand_row.get("expected_passengers"),
        "peak_demand_window_utc": demand_row.get("window_start"),
        "peak_wait_minutes": wait_row.get("estimated_wait_minutes"),
        "peak_wait_window_utc": wait_row.get("window_start"),
    }


def _sorted_counter(counter: Counter, limit: int | None = None) -> list[dict]:
    items = counter.most_common(limit)
    return [{"key": key, "count": count} for key, count in items]


def main() -> None:
    result = replay(
        FIXTURE_DIR, DB_PATH, NOW_UTC,
        airports=["IST"], reset_db=True, per_airport_now=False,
    )
    session = result["session"]
    try:
        flights = result["all_flights_by_airport"]["IST"]
        api = result["api_by_airport"]["IST"]
        departures = [f for f in flights if f.direction == DIRECTION_DEPARTURE]
        arrivals = [f for f in flights if f.direction == DIRECTION_ARRIVAL]

        hourly_departures = Counter(_local_hour(f) for f in departures)
        hourly_arrivals = Counter(_local_hour(f) for f in arrivals)
        departure_destinations = Counter(f.arr_iata for f in departures)
        arrival_origins = Counter(f.dep_iata for f in arrivals)
        status_counts = Counter(f.status for f in flights)
        aircraft_counts = Counter(f.aircraft_icao for f in flights)

        route_density = Counter()
        for flight in flights:
            route = flight.arr_iata if flight.direction == DIRECTION_DEPARTURE else flight.dep_iata
            route_density[(flight.direction, _local_hour(flight), route)] += 1

        domestic_international = {
            "departures": {
                "domestic": sum(f.location == LOCATION_DOMESTIC for f in departures),
                "international": sum(f.location == LOCATION_INTERNATIONAL for f in departures),
            },
            "arrivals": {
                "domestic": sum(f.location == LOCATION_DOMESTIC for f in arrivals),
                "international": sum(f.location == LOCATION_INTERNATIONAL for f in arrivals),
            },
        }

        graph_windows = {
            "overall": api["overall"]["windows"],
            "domestic_security": api["domestic_security"]["windows"],
            "departure_passport": api["international_departure"]["passport"]["windows"],
            "international_security": api["international_departure"]["security"]["windows"],
            "arrival_passport": api["international_arrival"]["windows"],
        }
        graph_bucket_counts = {name: len(rows) for name, rows in graph_windows.items()}
        assert all(count == 24 for count in graph_bucket_counts.values()), graph_bucket_counts
        assert all(f.dep_iata == "IST" for f in departures)
        assert all(f.arr_iata == "IST" for f in arrivals)
        assert all(f.aircraft_icao for f in flights)

        report = {
            "fixture_kind": "synthetic-derived-test-data",
            "published_timetable": False,
            "airport": "IST",
            "now_utc": NOW_UTC.isoformat(),
            "test_db": str(DB_PATH.relative_to(REPO_ROOT)),
            "totals": {"departures": len(departures), "arrivals": len(arrivals)},
            "hourly_departures_local": {str(h): hourly_departures[h] for h in range(24)},
            "hourly_arrivals_local": {str(h): hourly_arrivals[h] for h in range(24)},
            "busiest_departure_hours_local": [
                {"hour": hour, "count": count}
                for hour, count in hourly_departures.most_common(5)
            ],
            "busiest_arrival_hours_local": [
                {"hour": hour, "count": count}
                for hour, count in hourly_arrivals.most_common(5)
            ],
            "busiest_departure_destinations": _sorted_counter(departure_destinations, 12),
            "busiest_arrival_origins": _sorted_counter(arrival_origins, 12),
            "domestic_international": domestic_international,
            "status_counts": dict(sorted(status_counts.items())),
            "aircraft_mix": _sorted_counter(aircraft_counts),
            "route_density_examples": [
                {"direction": key[0], "local_hour": key[1], "route": key[2], "count": count}
                for key, count in route_density.most_common(15)
            ],
            "graph_bucket_counts": graph_bucket_counts,
            "queue_peaks": {
                "departure_passport": _peak(graph_windows["departure_passport"]),
                "domestic_security": _peak(graph_windows["domestic_security"]),
                "international_security": _peak(graph_windows["international_security"]),
                "arrival_passport": _peak(graph_windows["arrival_passport"]),
            },
            "pipeline": {
                "parsed_flights": result["parsed_flights"],
                "source_a_departures": result["source_a_dep_count"],
                "source_a_arrivals": result["source_a_arr_count"],
                "source_b": result["source_b_count"],
                "prediction_summary": result["prediction_summary"],
            },
        }
        REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        session.close()
        result["engine"].dispose()


if __name__ == "__main__":
    main()
