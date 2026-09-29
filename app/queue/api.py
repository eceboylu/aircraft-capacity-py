
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .constants import (
    DEMAND_WINDOW_MINUTES,
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_ORDER,
    RISK_UNKNOWN,
)
from .domain.operational_day import (
    operational_day_window,
    resolve_airport_timezone,
)
from .models import Airport, Flight, QueueAirportDepartureFlow5m, QueuePrediction, QueueWaitDisplay5m

RISK_TO_UI_LABEL = {
    RISK_LOW: "Normal",
    RISK_MEDIUM: "Getting Busy",
    RISK_HIGH: "Busy",
    RISK_CRITICAL: "Very Busy",
    RISK_UNKNOWN: "Unknown",
}


def ui_label_for_risk(risk: str | None) -> str | None:
    if risk is None:
        return None
    return RISK_TO_UI_LABEL.get(risk, risk)


def overall_status(*risks: str | None) -> dict:
    candidates = [r for r in risks if r is not None]
    if not candidates:
        return {"risk": None, "label": None}

    worst = max(candidates, key=lambda r: RISK_ORDER.get(r, -1))
    return {"risk": worst, "label": ui_label_for_risk(worst)}


def tracked_airports(session) -> list[str]:
    flight_codes = session.execute(
        select(Flight.airport_iata).distinct()
    ).scalars().all()
    prediction_codes = session.execute(
        select(QueuePrediction.airport_iata).distinct()
    ).scalars().all()
    codes = {code for code in (*flight_codes, *prediction_codes) if code}
    return sorted(codes)


_LOCAL_AIRPORT_NAME_FALLBACK: dict[str, str] = {}


def airport_directory(session) -> list[dict]:
    codes = tracked_airports(session)
    if not codes:
        return []

    primary_names = dict(session.execute(
        select(Airport.iata_code, Airport.airport_name)
        .where(Airport.iata_code.in_(codes))
    ).all())

    def resolve_name(code: str) -> str | None:
        return primary_names.get(code) or _LOCAL_AIRPORT_NAME_FALLBACK.get(code)

    return [{"iata": code, "name": resolve_name(code)} for code in codes]


def _floor_to_window(moment: datetime, window_minutes: int = DEMAND_WINDOW_MINUTES) -> datetime:
    minute = (moment.minute // window_minutes) * window_minutes
    return moment.replace(minute=minute, second=0, microsecond=0)


def traffic_breakdown(
    session, airport_iata: str, now: datetime | None = None
) -> dict | None:
    now = now if now is not None else _utcnow()

    rows = session.execute(
        select(Flight).where(
            Flight.airport_iata == airport_iata,
            Flight.status.notin_(EXCLUDED_STATUSES),
        )
    ).scalars().all()

    dated = []
    for flight in rows:
        moment = (
            flight.dep_scheduled_utc if flight.direction == DIRECTION_DEPARTURE
            else flight.arr_scheduled_utc
        )
        if moment is not None:
            dated.append((flight, moment))

    if not dated:
        return None

    window_starts = sorted({_floor_to_window(m) for _, m in dated})
    containing = [
        w for w in window_starts
        if w <= now < w + timedelta(minutes=DEMAND_WINDOW_MINUTES)
    ]
    if containing:
        chosen = containing[-1]
    else:
        past = [w for w in window_starts if w <= now]
        chosen = past[-1] if past else window_starts[0]

    window_end = chosen + timedelta(minutes=DEMAND_WINDOW_MINUTES)
    in_window = [f for f, m in dated if chosen <= m < window_end]

    return {
        "window_start": chosen.isoformat(),
        "window_end": window_end.isoformat(),
        "departures": sum(1 for f in in_window if f.direction == DIRECTION_DEPARTURE),
        "arrivals": sum(1 for f in in_window if f.direction == DIRECTION_ARRIVAL),
        "domestic": sum(1 for f in in_window if f.location == LOCATION_DOMESTIC),
        "international": sum(1 for f in in_window if f.location == LOCATION_INTERNATIONAL),
    }


def _to_local_iso(moment_utc: datetime, tz) -> str | None:
    if tz is None:
        return None
    aware_utc = moment_utc.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(tz).isoformat()


def _window_to_dict(row: QueuePrediction, tz=None) -> dict:
    return {
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "window_start_local": _to_local_iso(row.window_start, tz),
        "window_end_local": _to_local_iso(row.window_end, tz),
        "flight_count": row.flight_count,
        "expected_passengers": row.expected_passengers,
        "baseline_ratio": row.baseline_ratio,
        "flight_ratio": row.flight_ratio,
        "passenger_ratio": row.passenger_ratio,
        "utilization": row.utilization,
        "estimated_wait_minutes": row.estimated_wait_minutes,
        "risk": row.risk,
        "risk_label": ui_label_for_risk(row.risk),
        "confidence": row.confidence,
        "reasons": json.loads(row.reasons or "[]"),
        "calculated_at": row.calculated_at.isoformat(),
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _synthetic_zero_demand_window(window_start: datetime, window_end: datetime, tz=None) -> dict:
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_start_local": _to_local_iso(window_start, tz),
        "window_end_local": _to_local_iso(window_end, tz),
        "flight_count": 0,
        "expected_passengers": 0,
        "baseline_ratio": None,
        "flight_ratio": None,
        "passenger_ratio": None,
        "utilization": 0.0,
        "estimated_wait_minutes": 0.0,
        "risk": RISK_LOW,
        "risk_label": ui_label_for_risk(RISK_LOW),
        "confidence": 1.0,
        "reasons": [],
        "calculated_at": None,
    }


def _pick_highest_severity_entry(entries: list[dict]) -> dict:
    top_severity = max(RISK_ORDER.get(e["risk"], -1) for e in entries)
    top_entries = [e for e in entries if RISK_ORDER.get(e["risk"], -1) == top_severity]
    with_wait = [e for e in top_entries if e["estimated_wait_minutes"] is not None]
    return max(with_wait, key=lambda e: e["estimated_wait_minutes"]) if with_wait else top_entries[0]


def _merge_duplicate_local_hour(first: dict, second: dict) -> dict:
    winner = _pick_highest_severity_entry([first, second])
    calculated_candidates = [
        v for v in (first["calculated_at"], second["calculated_at"]) if v is not None
    ]
    return {
        "window_start": min(first["window_start"], second["window_start"]),
        "window_end": max(first["window_end"], second["window_end"]),
        "window_start_local": first.get("window_start_local"),
        "window_end_local": second.get("window_end_local"),
        "flight_count": first["flight_count"] + second["flight_count"],
        "expected_passengers": first["expected_passengers"] + second["expected_passengers"],
        "baseline_ratio": winner["baseline_ratio"],
        "flight_ratio": winner["flight_ratio"],
        "passenger_ratio": winner["passenger_ratio"],
        "utilization": winner["utilization"],
        "estimated_wait_minutes": winner["estimated_wait_minutes"],
        "risk": winner["risk"],
        "risk_label": winner["risk_label"],
        "confidence": winner["confidence"],
        "reasons": winner["reasons"],
        "calculated_at": max(calculated_candidates) if calculated_candidates else None,
    }


def _synthetic_gap_window(transition_utc: datetime, local_start_naive: datetime, local_end_naive: datetime) -> dict:
    key_iso = transition_utc.isoformat()
    return {
        "window_start": key_iso,
        "window_end": key_iso,
        "window_start_local": local_start_naive.isoformat(),
        "window_end_local": local_end_naive.isoformat(),
        "flight_count": 0,
        "expected_passengers": 0,
        "baseline_ratio": None,
        "flight_ratio": None,
        "passenger_ratio": None,
        "utilization": 0.0,
        "estimated_wait_minutes": 0.0,
        "risk": RISK_LOW,
        "risk_label": ui_label_for_risk(RISK_LOW),
        "confidence": 1.0,
        "reasons": [],
        "calculated_at": None,
    }


def _build_exact24_display_windows(
    windows: list[dict],
    grid_start: datetime,
    grid_end: datetime,
    day_start: datetime,
    tz,
) -> list[dict]:
    real_hours = int(round((grid_end - grid_start).total_seconds() / 3600))
    by_utc_start = {w["window_start"]: w for w in windows}

    def _entry_for(utc_hour: datetime) -> dict:
        key = utc_hour.isoformat()
        if key in by_utc_start:
            return by_utc_start[key]
        return _synthetic_zero_demand_window(utc_hour, utc_hour + timedelta(hours=1), tz)

    local_midnight_naive = day_start.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)

    display: list[dict] = []
    expected_label = 0
    i = 0
    while i < real_hours and expected_label < 24:
        utc_hour = grid_start + timedelta(hours=i)
        local_label = utc_hour.replace(tzinfo=timezone.utc).astimezone(tz).hour

        if local_label == expected_label:
            display.append(_entry_for(utc_hour))
            expected_label += 1
            i += 1
            continue

        if display and local_label == (expected_label - 1) % 24:
            previous = display.pop()
            display.append(_merge_duplicate_local_hour(previous, _entry_for(utc_hour)))
            i += 1
            continue

        gap_local_start = local_midnight_naive + timedelta(hours=expected_label)
        transition = utc_hour - timedelta(microseconds=1)
        display.append(_synthetic_gap_window(
            transition, gap_local_start, gap_local_start + timedelta(hours=1),
        ))
        expected_label += 1

    while expected_label < 24:
        transition = grid_start + timedelta(hours=real_hours) - timedelta(microseconds=1)
        gap_local_start = local_midnight_naive + timedelta(hours=expected_label)
        display.append(_synthetic_gap_window(
            transition, gap_local_start, gap_local_start + timedelta(hours=1),
        ))
        expected_label += 1

    return display


def _pad_series_to_24_hours(windows: list[dict], day_start: datetime, tz=None) -> list[dict]:
    by_start = {w["window_start"]: w for w in windows}
    for k in range(24):
        start = day_start + timedelta(hours=k)
        end = start + timedelta(hours=1)
        key = start.isoformat()
        if key not in by_start:
            by_start[key] = _synthetic_zero_demand_window(start, end, tz)
    return sorted(by_start.values(), key=lambda w: w["window_start"])


def _resolve_airport_tz_and_day_start(session, airport_iata: str, now: datetime):
    airport = session.get(Airport, airport_iata)
    if airport is None:
        return None, None, None
    tz = resolve_airport_timezone(airport.timezone)
    if tz is None:
        return None, None, None
    day_start, day_end = operational_day_window(tz, now)
    return tz, day_start, day_end


def _pick_current(rows: list[QueuePrediction], now: datetime) -> QueuePrediction | None:
    if not rows:
        return None

    containing = [r for r in rows if r.window_start <= now < r.window_end]
    if containing:
        return containing[-1]

    past = [r for r in rows if r.window_start <= now]
    if past:
        return past[-1]

    return rows[0]


def _display_point_to_dict(row: QueueWaitDisplay5m, tz=None) -> dict:
    return {
        "window_start": row.window_start.isoformat(),
        "window_start_local": _to_local_iso(row.window_start, tz),
        "estimated_wait_minutes": row.estimated_wait_minutes,
        # ADIM (Passenger-Weighted Display Wait) - frontend'in 30dk bar'ı
        # ARTIK 6 noktanın basit ortalaması değil, passenger-weighted
        # toplama - `to30MinuteVisualBuckets()` bu iki alanı kullanır.
        "wait_numerator": row.wait_numerator,
        "passenger_count": row.passenger_count,
        "risk": row.risk,
        "risk_label": ui_label_for_risk(row.risk),
    }


def display_5m_series(
    session, airport_iata: str, process: str,
    day_start: datetime | None = None, day_end: datetime | None = None, tz=None,
) -> list[dict]:
    query = select(QueueWaitDisplay5m).where(
        QueueWaitDisplay5m.airport_iata == airport_iata,
        QueueWaitDisplay5m.process == process,
    )
    if day_start is not None:
        query = query.where(QueueWaitDisplay5m.window_start >= day_start)
    if day_end is not None:
        query = query.where(QueueWaitDisplay5m.window_start < day_end)
    query = query.order_by(QueueWaitDisplay5m.window_start)
    rows = session.execute(query).scalars().all()
    return [_display_point_to_dict(row, tz) for row in rows]


# ---------------------------------------------------------------------------
# Airport Departure Passenger Flow - QUEUE WAIT DEĞİL. "Bu havalimanına
# şu an ne kadar departure yolcusu GELİYOR (show-up)?" - security/
# passport/backlog/remaining-wait hesaplarından TAMAMEN AYRI, salt-okunur
# bir congestion/flow serisi (bkz. audit.py:record_airport_departure_
# flow_5m). `queue_airport_departure_flow_5m` run_id bazlı/tarihsel
# (audit tablosu) olduğu için, bir havalimanı için EN SON run'ın
# satırları seçiliyor.
# ---------------------------------------------------------------------------

def _departure_flow_point_to_dict(row: QueueAirportDepartureFlow5m, tz=None) -> dict:
    return {
        "window_start": row.window_start_utc.isoformat(),
        "window_start_local": _to_local_iso(row.window_start_utc, tz),
        "window_end": row.window_end_utc.isoformat(),
        "window_end_local": _to_local_iso(row.window_end_utc, tz),
        "total_departure_pax": row.total_departure_pax,
        "domestic_departure_pax": row.domestic_departure_pax,
        "international_departure_pax": row.international_departure_pax,
        "schengen_departure_pax": row.schengen_departure_pax,
        "non_schengen_departure_pax": row.non_schengen_departure_pax,
        "flight_count": row.flight_count,
    }


def _latest_departure_flow_run_id(session, airport_iata: str) -> str | None:
    return session.execute(
        select(QueueAirportDepartureFlow5m.run_id)
        .where(QueueAirportDepartureFlow5m.airport_iata == airport_iata)
        .order_by(QueueAirportDepartureFlow5m.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def airport_departure_flow_5m_series(
    session, airport_iata: str,
    day_start: datetime | None = None, day_end: datetime | None = None, tz=None,
) -> dict:
    run_id = _latest_departure_flow_run_id(session, airport_iata)
    if run_id is None:
        return {"run_id": None, "calculation_method": None, "points": []}

    query = select(QueueAirportDepartureFlow5m).where(
        QueueAirportDepartureFlow5m.airport_iata == airport_iata,
        QueueAirportDepartureFlow5m.run_id == run_id,
    )
    if day_start is not None:
        query = query.where(QueueAirportDepartureFlow5m.window_start_utc >= day_start)
    if day_end is not None:
        query = query.where(QueueAirportDepartureFlow5m.window_start_utc < day_end)
    query = query.order_by(QueueAirportDepartureFlow5m.window_start_utc)
    rows = session.execute(query).scalars().all()

    return {
        "run_id": run_id,
        "calculation_method": rows[0].calculation_method if rows else None,
        "points": [_departure_flow_point_to_dict(row, tz) for row in rows],
    }


def process_series(
    session,
    airport_iata: str,
    process: str,
    since: datetime | None = None,
    now: datetime | None = None,
    day_start: datetime | None = None,
    tz=None,
    day_end: datetime | None = None,
) -> dict:
    now = now if now is not None else _utcnow()

    query = select(QueuePrediction).where(
        QueuePrediction.airport_iata == airport_iata,
        QueuePrediction.process == process,
    )
    if since is not None:
        query = query.where(QueuePrediction.window_start >= since)
    query = query.order_by(QueuePrediction.window_start)

    rows = session.execute(query).scalars().all()

    if day_start is not None:
        grid_start = day_start
        if grid_start.minute or grid_start.second or grid_start.microsecond:
            grid_start = grid_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

        if day_end is not None:
            grid_end = day_end
            if grid_end.minute or grid_end.second or grid_end.microsecond:
                grid_end = grid_end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            grid_end = grid_start + timedelta(hours=24)

        def _within_display_bounds(row: QueuePrediction) -> bool:
            return grid_start <= row.window_start < grid_end

        rows_for_windows = [r for r in rows if _within_display_bounds(r)]
        windows = [_window_to_dict(row, tz) for row in rows_for_windows]
        windows = _build_exact24_display_windows(windows, grid_start, grid_end, day_start, tz)
        current = _pick_current_window(windows, now)
    else:
        windows = [_window_to_dict(row, tz) for row in rows]
        current_row = _pick_current(rows, now)
        current = _window_to_dict(current_row, tz) if current_row is not None else None

    return {
        "process": process,
        "current": current,
        "windows": windows,
    }


def _pick_current_window(windows: list[dict], now: datetime) -> dict | None:
    if not windows:
        return None

    containing = [
        w for w in windows
        if datetime.fromisoformat(w["window_start"]) <= now < datetime.fromisoformat(w["window_end"])
    ]
    if containing:
        return containing[-1]

    past = [w for w in windows if datetime.fromisoformat(w["window_start"]) <= now]
    if past:
        return past[-1]

    return windows[0]


def _international_departure_split(
    passport_departure: dict, international_security: dict,
) -> dict:
    return {
        "process": "international_departure",
        "passport": passport_departure,
        "security": international_security,
    }


def airport_predictions(
    session, airport_iata: str, since: datetime | None = None, now: datetime | None = None
) -> dict:
    now = now if now is not None else _utcnow()
    tz, day_start, day_end = _resolve_airport_tz_and_day_start(session, airport_iata, now)

    security = process_series(session, airport_iata, PROCESS_SECURITY, since, now)
    passport = process_series(session, airport_iata, PROCESS_PASSPORT, since, now)
    domestic_security = process_series(session, airport_iata, PROCESS_SECURITY_DOMESTIC, since, now, day_start, tz, day_end)
    international_security = process_series(session, airport_iata, PROCESS_SECURITY_INTL, since, now, day_start, tz, day_end)
    passport_departure = process_series(session, airport_iata, PROCESS_PASSPORT_DEPARTURE, since, now, day_start, tz, day_end)
    passport_arrival = process_series(session, airport_iata, PROCESS_PASSPORT_ARRIVAL, since, now, day_start, tz, day_end)

    domestic_security["display_5m"] = display_5m_series(session, airport_iata, PROCESS_SECURITY_DOMESTIC, day_start, day_end, tz)
    international_security["display_5m"] = display_5m_series(session, airport_iata, PROCESS_SECURITY_INTL, day_start, day_end, tz)
    passport_departure["display_5m"] = display_5m_series(session, airport_iata, PROCESS_PASSPORT_DEPARTURE, day_start, day_end, tz)
    passport_arrival["display_5m"] = display_5m_series(session, airport_iata, PROCESS_PASSPORT_ARRIVAL, day_start, day_end, tz)

    international_departure = _international_departure_split(
        passport_departure, international_security,
    )

    return {
        "airport": airport_iata,
        "breakdown": traffic_breakdown(session, airport_iata, now),
        "domestic_security": domestic_security,
        "international_security": international_security,
        "international_passport": passport,
        "international_departure": international_departure,
        "international_arrival": passport_arrival,
        "security": security,
        "passport": passport,
        # ADIM (Airport Departure Passenger Flow) - queue wait DEĞİL,
        # ayrı/bağımsız bir congestion serisi (bkz. app/queue/api.py
        # üstündeki modül yorumu).
        "departure_flow": airport_departure_flow_5m_series(session, airport_iata, day_start, day_end, tz),
    }
