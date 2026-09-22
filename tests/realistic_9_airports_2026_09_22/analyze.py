"""
ADIM (Full-Scale 9-Airport Measurement) - `generate_fixture.py`'nin
ürettiği fixture'ı GERÇEK production pipeline'dan (`tests/date_shift_
replay/run_date_shift_replay.py:replay()` - izole SQLite, gerçek
`database.sqlite`'a DOKUNMAZ) geçirir ve kullanıcının istediği tüm
ölçümleri/kontrolleri (Bölüm 5-10) çıkarır.

SADECE ÖLÇÜM - hiçbir production dosyası (`engine.py`/`config.py`/
`scoring.py`/`event_queue.py`/`airport_scale.py`) BU BETİK TARAFINDAN
DEĞİŞTİRİLMEZ, SADECE import edilip ÇAĞRILIR.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.date_shift_replay.run_date_shift_replay import replay  # noqa: E402

from app.queue.config import get_config  # noqa: E402
from app.queue.constants import (  # noqa: E402
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from app.queue.core.event_queue import total_count  # noqa: E402
from app.queue.core.scoring import (  # noqa: E402
    domestic_security_capacity_rate,
    international_security_capacity_rate,
    passport_departure_capacity_rate,
    passport_arrival_capacity_rate,
)
from app.queue.domain.demand import (  # noqa: E402
    DemandCalculator,
    arrival_passenger_release_events,
    departure_show_up_events,
)
from app.queue.domain.dynamic_staffing import effective_capacity_by_hour  # noqa: E402
from app.queue.domain.occupancy_calibration import occupancy_factor_for  # noqa: E402
from app.queue.engine import _event_driven_queue_demand  # noqa: E402
from app.service import AircraftCapacityService  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent
DB_PATH = FIXTURE_DIR / "realistic_9_airports.sqlite"
NOW_UTC = datetime(2026, 9, 22, 19, 0)
AIRPORTS = ["IST", "SAW", "AMS", "JMK", "TZX", "KOI", "ISC", "SOG", "JTY"]
LARGE = {"IST", "SAW", "AMS"}
GRAPH_NAMES = [
    "overall", "domestic_security",
    "international_departure_passport", "international_departure_security",
    "international_arrival",
]

# Bölüm 4/6 - araştırılmış gerçek pax/movement (kaynak/tarih detayı
# `app/queue/domain/occupancy_calibration.py`'nin docstring'inde).
# SADECE raporlama için - kalibrasyon FAKTÖRÜ zaten production
# `occupancy_calibration.AIRPORT_OCCUPANCY_FACTORS`'ta.
REAL_PAX_PER_MOVEMENT = {
    "IST": 156.19, "SAW": 175.66, "AMS": 144.05,
    "JMK": 93.36, "TZX": 146.17, "KOI": 13.40,
    "ISC": 7.497, "SOG": 12.25, "JTY": 22.74,
}


def _graph_windows(api: dict) -> dict:
    return {
        "overall": api["overall"]["windows"],
        "domestic_security": api["domestic_security"]["windows"],
        "international_departure_passport": api["international_departure"]["passport"]["windows"],
        "international_departure_security": api["international_departure"]["security"]["windows"],
        "international_arrival": api["international_arrival"]["windows"],
    }


def _peak_wait(rows: list[dict]) -> dict:
    if not rows:
        return {"peak_wait_minutes": None, "peak_utilization": None, "peak_risk": None}
    wait_row = max(rows, key=lambda r: r.get("estimated_wait_minutes") or 0)
    util_row = max(rows, key=lambda r: r.get("utilization") or 0)
    risk_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3, "UNKNOWN": -1}
    risk_row = max(rows, key=lambda r: risk_order.get(r.get("risk"), -1))
    return {
        "peak_wait_minutes": wait_row.get("estimated_wait_minutes"),
        "peak_wait_window": wait_row.get("window_start"),
        "peak_utilization": util_row.get("utilization"),
        "peak_risk": risk_row.get("risk"),
    }


def main() -> None:
    result = replay(
        FIXTURE_DIR, DB_PATH, NOW_UTC, airports=None, reset_db=True, per_airport_now=False,
    )
    session = result["session"]
    try:
        report: dict = {
            "now_utc": NOW_UTC.isoformat(),
            "total_generated_flights": result["parsed_flights"],
            "refresh": result["refresh"],
            "prediction_summary": {
                "airports": result["prediction_summary"]["airports"],
                "predictions": result["prediction_summary"]["predictions"],
                "failed_airports": result["prediction_summary"]["failed_airports"],
            },
            "per_airport": {},
            "exact24": {},
            "conservation": {},
            "long_wait_check": {},
            "security_bottleneck": {},
        }

        gen_summary = json.loads((FIXTURE_DIR / "_generation_summary.json").read_text(encoding="utf-8"))
        gen_by_airport = {row["airport"]: row for row in gen_summary}

        exact24_pass = 0
        exact24_total = 0
        exact24_fail_detail = []

        for code in AIRPORTS:
            flights = result["all_flights_by_airport"][code]
            api = result["api_by_airport"][code]
            config = get_config(session, code)
            resolver = AircraftCapacityService(session)
            demand = DemandCalculator(resolver)

            departures = [f for f in flights if f.direction == DIRECTION_DEPARTURE]
            arrivals = [f for f in flights if f.direction == DIRECTION_ARRIVAL]

            windows = _graph_windows(api)
            for gname, rows in windows.items():
                exact24_total += 1
                ok = len(rows) == 24
                if ok:
                    exact24_pass += 1
                else:
                    exact24_fail_detail.append(f"{code}/{gname}: {len(rows)} buckets")

            entry = {
                "research_movements": gen_by_airport[code]["research_target_movements"],
                "generated_movements": gen_by_airport[code]["generated_movements"],
                "diff_pct": round(
                    (gen_by_airport[code]["generated_movements"] - gen_by_airport[code]["research_target_movements"])
                    / gen_by_airport[code]["research_target_movements"] * 100, 2,
                ),
                "departures": len(departures),
                "arrivals": len(arrivals),
                "before_modeled_passengers_day": gen_by_airport[code]["modeled_passengers_day"],
                "real_pax_per_movement": REAL_PAX_PER_MOVEMENT.get(code),
                "real_passengers_day_at_this_volume": (
                    round(REAL_PAX_PER_MOVEMENT[code] * gen_by_airport[code]["generated_movements"], 1)
                    if code in REAL_PAX_PER_MOVEMENT else None
                ),
                "calibration_factor": occupancy_factor_for(code)[0],
                "calibration_level": occupancy_factor_for(code)[1],
                "peak": {
                    "domestic_security": _peak_wait(windows["domestic_security"]),
                    "departure_passport": _peak_wait(windows["international_departure_passport"]),
                    "international_security": _peak_wait(windows["international_departure_security"]),
                    "arrival_passport": _peak_wait(windows["international_arrival"]),
                },
            }

            # --- Dynamic staffing schedules (LARGE only) -------------------
            coupling = _event_driven_queue_demand(flights, config, demand, now=NOW_UTC)
            schedules = coupling.get("passport_schedules", {})
            dep_schedule = schedules.get(PROCESS_PASSPORT_DEPARTURE)
            arr_schedule = schedules.get(PROCESS_PASSPORT_ARRIVAL)
            if code in LARGE:
                entry["peak_departure_active_passport_servers"] = (
                    max(c for _, c in dep_schedule) if dep_schedule else config.passport_departure_server_count
                )
                entry["peak_arrival_active_passport_servers"] = (
                    max(c for _, c in arr_schedule) if arr_schedule else config.passport_arrival_server_count
                )

            report["per_airport"][code] = entry

            # --- Conservation checks --------------------------------------
            dep_total_demand = sum(demand.passenger_demand(f) for f in departures)
            dep_showup_total = sum(
                amount for f in departures for _, amount in departure_show_up_events(f, demand.passenger_demand(f))
            )
            arr_total_demand = sum(demand.passenger_demand(f) for f in arrivals)
            arr_release_total = sum(
                amount for f in arrivals for _, amount in arrival_passenger_release_events(f, demand.passenger_demand(f))
            )

            entry["after_modeled_passengers_day"] = round(dep_total_demand + arr_total_demand, 1)
            entry["difference_vs_real_pct"] = (
                round(
                    (entry["after_modeled_passengers_day"] - entry["real_passengers_day_at_this_volume"])
                    / entry["real_passengers_day_at_this_volume"] * 100, 2,
                )
                if entry["real_passengers_day_at_this_volume"] else None
            )

            process_events = coupling.get("process_events", {})
            passport_dep_out = total_count(process_events.get(PROCESS_PASSPORT_DEPARTURE, []))
            security_intl_in = total_count(process_events.get(PROCESS_SECURITY_INTL, []))
            report["conservation"][code] = {
                "departure_original_demand": round(dep_total_demand, 3),
                "departure_showup_sum": round(dep_showup_total, 3),
                "departure_conserved": abs(dep_total_demand - dep_showup_total) < 1e-6,
                "arrival_original_demand": round(arr_total_demand, 3),
                "arrival_release_sum": round(arr_release_total, 3),
                "arrival_conserved": abs(arr_total_demand - arr_release_total) < 1e-6,
                "departure_passport_output": round(passport_dep_out, 3),
                "international_security_input": round(security_intl_in, 3),
                "passport_to_security_conserved": abs(passport_dep_out - security_intl_in) < 1e-6,
            }

            # --- IST deep dive ----------------------------------------------
            if code == "IST":
                report["ist_deep_dive"] = _ist_deep_dive(config, coupling, dep_schedule, arr_schedule, windows)

            # --- Security bottleneck check -----------------------------------
            dom_cap_hour = domestic_security_capacity_rate(config) * 60
            intl_cap_hour = international_security_capacity_rate(config) * 60
            dom_peak_incoming = max(
                (r.get("expected_passengers") or 0 for r in windows["domestic_security"]), default=0,
            )
            intl_peak_incoming = max(
                (r.get("expected_passengers") or 0 for r in windows["international_departure_security"]), default=0,
            )
            report["security_bottleneck"][code] = {
                "domestic_security_capacity_per_hour": round(dom_cap_hour, 1),
                "domestic_security_peak_incoming_per_hour": dom_peak_incoming,
                "domestic_bottleneck": dom_peak_incoming > dom_cap_hour,
                "international_security_capacity_per_hour": round(intl_cap_hour, 1),
                "international_security_peak_incoming_per_hour": intl_peak_incoming,
                "international_bottleneck": intl_peak_incoming > intl_cap_hour,
            }

            # --- 20+ hour wait / multi-day backlog check ---------------------
            if code in ("IST", "SAW", "AMS", "JMK"):
                all_waits = []
                for gname, rows in windows.items():
                    for r in rows:
                        if r.get("estimated_wait_minutes") is not None:
                            all_waits.append((gname, r["window_start"], r["estimated_wait_minutes"]))
                max_wait = max(all_waits, key=lambda t: t[2]) if all_waits else None
                report["long_wait_check"][code] = {
                    "max_wait_minutes": max_wait[2] if max_wait else None,
                    "max_wait_graph": max_wait[0] if max_wait else None,
                    "max_wait_window": max_wait[1] if max_wait else None,
                    "wait_20h_plus": bool(max_wait and max_wait[2] >= 1200),
                    "wait_1000min_plus": bool(max_wait and max_wait[2] >= 1000),
                }

        report["exact24"]["pass"] = exact24_pass
        report["exact24"]["total"] = exact24_total
        report["exact24"]["fail_detail"] = exact24_fail_detail

        out_path = FIXTURE_DIR / "measurement_report.json"
        out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, indent=2, default=str))
    finally:
        session.close()
        result["engine"].dispose()


def _ist_deep_dive(config, coupling, dep_schedule, arr_schedule, windows) -> dict:
    demand_by_process = coupling.get("demand_by_hour", {})
    dep_demand_series = demand_by_process.get(PROCESS_PASSPORT_DEPARTURE, {})
    arr_demand_series = demand_by_process.get(PROCESS_PASSPORT_ARRIVAL, {})
    dom_sec_demand_series = demand_by_process.get(PROCESS_SECURITY_DOMESTIC, {})
    intl_sec_demand_series = demand_by_process.get(PROCESS_SECURITY_INTL, {})

    starts = sorted(dep_demand_series.keys() | arr_demand_series.keys() | dom_sec_demand_series.keys() | intl_sec_demand_series.keys())
    dep_eff = effective_capacity_by_hour(dep_schedule, starts, 60) if dep_schedule else {}
    arr_eff = effective_capacity_by_hour(arr_schedule, starts, 60) if arr_schedule else {}

    service_rate_per_server_per_hour = 60 / config.passport_service_time_minutes

    return {
        "domestic_security": {
            "daily_demand": round(sum(dom_sec_demand_series.values()), 1),
            "peak_hourly_demand": round(max(dom_sec_demand_series.values(), default=0), 1),
            "capacity_per_hour": round(domestic_security_capacity_rate(config) * 60, 1),
            "peak_wait": _peak_wait(windows["domestic_security"])["peak_wait_minutes"],
            "peak_utilization": _peak_wait(windows["domestic_security"])["peak_utilization"],
        },
        "departure_passport": {
            "daily_demand": round(sum(dep_demand_series.values()), 1),
            "peak_hourly_incoming": round(max(dep_demand_series.values(), default=0), 1),
            "peak_active_servers": max((c for _, c in dep_schedule), default=config.passport_departure_server_count) if dep_schedule else config.passport_departure_server_count,
            "effective_capacity_per_hour_peak": round(max(dep_eff.values(), default=0) * service_rate_per_server_per_hour, 1) if dep_eff else round(config.passport_departure_server_count * service_rate_per_server_per_hour, 1),
            "peak_wait": _peak_wait(windows["international_departure_passport"])["peak_wait_minutes"],
            "peak_utilization": _peak_wait(windows["international_departure_passport"])["peak_utilization"],
        },
        "international_security": {
            "daily_incoming": round(sum(intl_sec_demand_series.values()), 1),
            "capacity_per_hour": round(international_security_capacity_rate(config) * 60, 1),
            "peak_incoming_per_hour": round(max(intl_sec_demand_series.values(), default=0), 1),
            "peak_wait": _peak_wait(windows["international_departure_security"])["peak_wait_minutes"],
            "peak_utilization": _peak_wait(windows["international_departure_security"])["peak_utilization"],
        },
        "arrival_passport": {
            "daily_demand": round(sum(arr_demand_series.values()), 1),
            "peak_active_servers": max((c for _, c in arr_schedule), default=config.passport_arrival_server_count) if arr_schedule else config.passport_arrival_server_count,
            "effective_capacity_per_hour_peak": round(max(arr_eff.values(), default=0) * service_rate_per_server_per_hour, 1) if arr_eff else round(config.passport_arrival_server_count * service_rate_per_server_per_hour, 1),
            "peak_wait": _peak_wait(windows["international_arrival"])["peak_wait_minutes"],
            "peak_utilization": _peak_wait(windows["international_arrival"])["peak_utilization"],
        },
    }


if __name__ == "__main__":
    main()
