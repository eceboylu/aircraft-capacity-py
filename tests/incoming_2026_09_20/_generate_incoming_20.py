"""
ADIM (20.09.2026 SAME-DEMAND AIRPORT SCALE COMPARISON) - IST'nin 19
Eylül'de kurulan high-density international departure pattern'ını
(28 flight, gerçek aircraft/timing dizisi) BİREBİR AYNI aircraft
sequence + timing + passenger demand ile 20 Eylül 2026 operasyonel
gününe, VE aynı zamanda gerçek large/medium/small/unknown havalimanlarına
(IST/CBR/MFG/OAG) taşır.

Amaç SADECE resource-capacity karşılaştırması (Bölüm 6) - route
gerçekliği ikincil, bu yüzden her havalimanı KENDİ gerçek (ama sabit,
tek) uluslararası partnerine uçar; aircraft ICAO SEQUENCE'ı (dolayısıyla
passenger demand) TÜM havalimanlarında BİREBİR AYNI kalır.

Risk/wait/utilization gibi HİÇBİR OUTPUT alanı burada YAZILMAZ - sadece
INPUT flight kayıtları. Production `data/*`/`database.sqlite` HİÇ
açılmadı/yazılmadı.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_DIR = Path(__file__).resolve().parent
REPO_ROOT = OUT_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.queue.domain.operational_day import resolve_airport_timezone  # noqa: E402

OP_DATE = datetime(2026, 9, 20)

# --- Gerçek airport sabitleri (flight_airports.sql'de doğrulandı) -----
IST = ("IST", "LTFM", "Europe/Istanbul", "TK", "THY", 7000)
CBR = ("CBR", "YSCB", "Australia/Sydney", "QF", "QFA", 7100)
MFG = ("MFG", "OPMF", "Asia/Karachi", "PK", "PIA", 7200)
OAG = ("OAG", "YORG", "Australia/Sydney", "VA", "VOZ", 7300)

# Her havalimanının KENDİ, GERÇEK (flight_airports.sql'de doğrulanmış),
# TEK uluslararası partneri - route gerçekliği ikincil (Bölüm 6), sadece
# aircraft type SEQUENCE demand'i belirler, destination BELİRLEMEZ.
PARTNER_BY_AIRPORT = {
    "IST": ("ZRH", "LSZH", "Europe/Zurich"),
    "CBR": ("SIN", "WSSS", "Asia/Singapore"),
    "MFG": ("DXB", "OMDB", "Asia/Dubai"),
    "OAG": ("DXB", "OMDB", "Asia/Dubai"),
}

# ADIM 19-Sep'teki GERÇEK IST high-density pattern'ının BİREBİR
# kopyası (bkz. tests/incoming_2026_09_19/Delays - Type Departures.json
# - 2026-09-19 için extract edilmiş, kronolojik, YEREL saat + aircraft
# tipi dizisi). None aircraft (enrichment testi, sadece IST 19 Sep'e
# özgüydü) burada sabit "A321" ile değiştirildi - demand AYNI (Source B
# zaten bu flight'ı A321'e çözüyordu).
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
    """Gerçek şema - `tests/incoming_2026_09_19/_generate_incoming.py:_record()` ile AYNI alan seti."""
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


def build() -> tuple[list[dict], list[dict], list[dict]]:
    departures: list[dict] = []
    arrivals: list[dict] = []
    live: list[dict] = []

    for iata, icao, tz_name, airline_iata, airline_icao, number_base in AIRPORTS:
        tz = resolve_airport_timezone(tz_name)
        partner_iata, partner_icao, partner_tz_name = PARTNER_BY_AIRPORT[iata]
        partner_tz = resolve_airport_timezone(partner_tz_name)

        num = number_base
        for hour, minute, aircraft in DENSE_PATTERN:
            num += 1
            duration = 300 if aircraft in LONG_HAUL_AIRCRAFT else 195
            departures.append(_record(
                airline_iata=airline_iata, airline_icao=airline_icao, flight_number=str(num),
                dep_iata=iata, dep_icao=icao, dep_tz=tz,
                dep_local_time=OP_DATE.replace(hour=hour, minute=minute),
                arr_iata=partner_iata, arr_icao=partner_icao, arr_tz=partner_tz,
                duration_minutes=duration, aircraft_icao=aircraft,
            ))

        # Bölüm 25/9 - International Arrival grafiğinin de boş kalmaması
        # için, her havalimanına AYRICA (fairness karşılaştırmasının
        # KAPSAMI DIŞINDA - Bölüm 4 SADECE departure pattern'ı için
        # "same demand" istiyor) 1 gerçek uluslararası varış eklenir.
        num += 1
        arr_local = OP_DATE.replace(hour=11, minute=0)
        arrivals.append(_record(
            airline_iata=airline_iata, airline_icao=airline_icao, flight_number=str(num),
            dep_iata=partner_iata, dep_icao=partner_icao, dep_tz=partner_tz,
            dep_local_time=arr_local - timedelta(minutes=195),
            arr_iata=iata, arr_icao=icao, arr_tz=tz,
            duration_minutes=195, aircraft_icao="A320",
        ))

    return departures, arrivals, live


def main() -> None:
    departures, arrivals, live = build()

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

    print(f"departures: {len(departures)}")
    print(f"arrivals: {len(arrivals)}")
    print(f"flights_live: {len(live)}")


if __name__ == "__main__":
    main()
