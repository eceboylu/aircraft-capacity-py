"""
ADIM (BUGUNUN TARIHI ICIN AYNI-DEMAND AIRPORT FLIGHT DATA) - 20 Eylul
same-demand generator'inin (`tests/incoming_2026_09_20/_generate_incoming_20.py`)
AYNI DENSE_PATTERN'ini (28 flight, aircraft ICAO sirasi, yerel saat
araliklari, bank yapisi) BIREBIR AYNI sekilde IST/CBR/MFG/OAG'a, ama
BUGUNUN (script CALISTIRILDIGI ANDAKI GERCEK) tarihine tasir.

HARD-CODE YOK: hedef takvim gunu her havalimani icin KENDI
`Airport.timezone` + `operational_date(tz, now_utc)` (production'in
KENDI operasyonel-gun cozumleyicisi) ile, script calistirildigi anda
BAGIMSIZ hesaplanir - "20.09" gibi sabit bir tarih YOK.

Cikis klasoru KENDI ADI DA dinamik (bu dosyanin bulundugu klasor -
cagiran taraf klasoru "bugun"un tarihiyle adlandirmis olmali,
ornegin tests/incoming_2026_09_21/).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_DIR = Path(__file__).resolve().parent
REPO_ROOT = OUT_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.queue.domain.operational_day import operational_date, resolve_airport_timezone  # noqa: E402

# Gercek, "su an" (UTC) - script calistirildigi anda ONE KEZ okunur.
GENERATION_NOW_UTC = datetime.now(dt_timezone.utc).replace(tzinfo=None)

# --- Gercek airport sabitleri (flight_airports.sql'de dogrulandi;
#     tests/incoming_2026_09_20/_generate_incoming_20.py ile AYNI) ----
IST = ("IST", "LTFM", "Europe/Istanbul", "TK", "THY", 8000)
CBR = ("CBR", "YSCB", "Australia/Sydney", "QF", "QFA", 8100)
MFG = ("MFG", "OPMF", "Asia/Karachi", "PK", "PIA", 8200)
OAG = ("OAG", "YORG", "Australia/Sydney", "VA", "VOZ", 8300)

PARTNER_BY_AIRPORT = {
    "IST": ("ZRH", "LSZH", "Europe/Zurich"),
    "CBR": ("SIN", "WSSS", "Asia/Singapore"),
    "MFG": ("DXB", "OMDB", "Asia/Dubai"),
    "OAG": ("DXB", "OMDB", "Asia/Dubai"),
}

# 20 Eylul'deki AYNI IST high-density international-departure pattern'inin
# BIREBIR kopyasi (bkz. tests/incoming_2026_09_20/_generate_incoming_20.py
# DENSE_PATTERN'i - flight sayisi/aircraft sequence/timing/bank yapisi
# BIREBIR AYNI kalir, SADECE tarih bugune tasinir).
DENSE_PATTERN = [
    (2, 35, "B738"), (5, 35, "B789"), (6, 0, "A321"), (6, 10, "B738"),
    (6, 15, "B789"), (6, 20, "A320"), (6, 30, "A321"), (6, 35, "A321"),
    (6, 40, "B738"), (6, 50, "A333"), (9, 35, "A321"), (10, 5, "A321"),
    (12, 0, "A320"), (12, 8, "A321"), (12, 14, "B738"), (13, 35, "B738"),
    (16, 35, "A321"), (17, 5, "B789"), (17, 20, "A333"), (17, 40, "B789"),
    (17, 55, "A321"), (18, 10, "B738"), (18, 25, "A320"), (18, 35, "B789"),
    (18, 50, "A321"), (19, 5, "B738"), (19, 35, "A320"), (21, 35, "A333"),
]

AIRPORTS = [IST, CBR, MFG, OAG]


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _record(
    *, airline_iata, airline_icao, flight_number,
    dep_iata, dep_icao, dep_tz, dep_local_time,
    arr_iata, arr_icao, arr_tz, duration_minutes, aircraft_icao,
) -> dict:
    """Gercek sema - `_generate_incoming_20.py:_record()` ile AYNI alan seti."""
    dep_local = dep_local_time.replace(tzinfo=dep_tz)
    dep_utc = dep_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    arr_local_naive = dep_local_time + timedelta(minutes=duration_minutes)
    arr_local = arr_local_naive.replace(tzinfo=dep_tz).astimezone(arr_tz)
    arr_utc = arr_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    arr_local_naive_display = arr_local.replace(tzinfo=None)

    flight_iata = f"{airline_iata}{flight_number}"
    flight_icao_code = f"{airline_icao}{flight_number}"

    return {
        "airline_iata": airline_iata,
        "airline_icao": airline_icao,
        "flight_iata": flight_iata,
        "flight_icao": flight_icao_code,
        "flight_number": flight_number,
        "dep_iata": dep_iata,
        "dep_icao": dep_icao,
        "dep_terminal": "1",
        "dep_gate": None,
        "dep_time": _fmt(dep_local_time),
        "dep_time_utc": _fmt(dep_utc),
        "dep_estimated": _fmt(dep_local_time),
        "dep_estimated_utc": _fmt(dep_utc),
        "dep_actual": _fmt(dep_local_time),
        "dep_actual_utc": _fmt(dep_utc),
        "arr_iata": arr_iata,
        "arr_icao": arr_icao,
        "arr_terminal": None,
        "arr_gate": None,
        "arr_baggage": None,
        "arr_time": _fmt(arr_local_naive_display),
        "arr_time_utc": _fmt(arr_utc),
        "arr_estimated": _fmt(arr_local_naive_display),
        "arr_estimated_utc": _fmt(arr_utc),
        "arr_actual": _fmt(arr_local_naive_display),
        "arr_actual_utc": _fmt(arr_utc),
        "cs_airline_iata": None,
        "cs_flight_number": None,
        "cs_flight_iata": None,
        "status": "landed",
        "duration": duration_minutes,
        "delayed": 0,
        "dep_delayed": 0,
        "arr_delayed": 0,
        "aircraft_icao": aircraft_icao,
        "arr_time_ts": int(arr_utc.replace(tzinfo=ZoneInfo("UTC")).timestamp()),
        "dep_time_ts": int(dep_utc.replace(tzinfo=ZoneInfo("UTC")).timestamp()),
        "arr_estimated_ts": int(arr_utc.replace(tzinfo=ZoneInfo("UTC")).timestamp()),
        "dep_estimated_ts": int(dep_utc.replace(tzinfo=ZoneInfo("UTC")).timestamp()),
        "arr_actual_ts": int(arr_utc.replace(tzinfo=ZoneInfo("UTC")).timestamp()),
        "dep_actual_ts": int(dep_utc.replace(tzinfo=ZoneInfo("UTC")).timestamp()),
    }


LONG_HAUL_AIRCRAFT = {"A333", "B789"}


def build() -> tuple[list[dict], list[dict], list[dict], dict[str, str]]:
    departures: list[dict] = []
    arrivals: list[dict] = []
    live: list[dict] = []
    resolved_op_dates: dict[str, str] = {}

    for iata, icao, tz_name, airline_iata, airline_icao, number_base in AIRPORTS:
        tz = resolve_airport_timezone(tz_name)
        # HARD-CODE YOK: bu havalimaninin KENDI "bugun"u, script'in
        # calistirildigi GERCEK ana (`GENERATION_NOW_UTC`) gore, production'in
        # KENDI `operational_date()` fonksiyonuyla cozulur.
        op_date = operational_date(tz, GENERATION_NOW_UTC)
        op_date_dt = datetime(op_date.year, op_date.month, op_date.day)
        resolved_op_dates[iata] = str(op_date)

        partner_iata, partner_icao, partner_tz_name = PARTNER_BY_AIRPORT[iata]
        partner_tz = resolve_airport_timezone(partner_tz_name)

        num = number_base
        for hour, minute, aircraft in DENSE_PATTERN:
            num += 1
            duration = 300 if aircraft in LONG_HAUL_AIRCRAFT else 195
            departures.append(_record(
                airline_iata=airline_iata, airline_icao=airline_icao, flight_number=str(num),
                dep_iata=iata, dep_icao=icao, dep_tz=tz,
                dep_local_time=op_date_dt.replace(hour=hour, minute=minute),
                arr_iata=partner_iata, arr_icao=partner_icao, arr_tz=partner_tz,
                duration_minutes=duration, aircraft_icao=aircraft,
            ))

        # Bolum 25/9 (20 Eylul ADIM'iyla AYNI) - International Arrival
        # grafiginin de bos kalmamasi icin 1 gercek uluslararasi varis.
        num += 1
        arr_local = op_date_dt.replace(hour=11, minute=0)
        arrivals.append(_record(
            airline_iata=airline_iata, airline_icao=airline_icao, flight_number=str(num),
            dep_iata=partner_iata, dep_icao=partner_icao, dep_tz=partner_tz,
            dep_local_time=arr_local - timedelta(minutes=195),
            arr_iata=iata, arr_icao=icao, arr_tz=tz,
            duration_minutes=195, aircraft_icao="A320",
        ))

    return departures, arrivals, live, resolved_op_dates


def main() -> None:
    departures, arrivals, live, resolved_op_dates = build()

    dep_payload = {
        "request": {"host": "airlabs.co", "method": "schedules", "has_more": False},
        "response": departures,
        "terms": "https://airlabs.co/docs/terms (test fixture)",
    }
    arr_payload = {
        "request": {"host": "airlabs.co", "method": "schedules", "has_more": False},
        "response": arrivals,
        "terms": dep_payload["terms"],
    }
    live_payload = {
        "request": {"host": "airlabs.co", "method": "flights", "has_more": False},
        "response": live,
        "terms": dep_payload["terms"],
    }

    (OUT_DIR / "Delays - Type Departures.json").write_text(
        json.dumps(dep_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    (OUT_DIR / "Delays - Type Arrivals.json").write_text(
        json.dumps(arr_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    (OUT_DIR / "flights_live.json").write_text(
        json.dumps(live_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )

    print(f"generation_now_utc: {GENERATION_NOW_UTC.isoformat()}")
    print(f"resolved local operational dates per airport: {resolved_op_dates}")
    print(f"departures: {len(departures)}")
    print(f"arrivals: {len(arrivals)}")
    print(f"flights_live: {len(live)}")


if __name__ == "__main__":
    main()
