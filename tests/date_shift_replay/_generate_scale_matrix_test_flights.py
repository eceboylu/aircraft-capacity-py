"""
ADIM (Airport Scale Test Matrix) - bu betik, TEK SEFERLİK (idempotent -
flight_iata prefix'i kontrol ediyor, tekrar çalıştırılırsa DUPLICATE
eklemez) olarak `tests/date_shift_replay/Delays - Type Departures.json`
/ `Delays - Type Arrivals.json` dosyalarına, GERÇEK AirLabs şemasıyla
(bkz. `_generate_extra_test_flights.py` - AYNI desen/alan adları) yeni
test flight'ları EKLER.

SADECE bu klasördeki (`tests/date_shift_replay/`) shifted KOPYA
dosyalarına yazar - gerçek `data/*.json` HİÇ AÇILMAZ/YAZILMAZ.

Amaç (kullanıcı talimatı - ADIM "Queue sistemini production öncesi
kapsamlı doğrula", Phase 3-6):
  - MEDIUM ölçek gerçek test airport'u: CBR (Canberra, YSCB,
    Australia/Sydney) - `data/orta_olcekli_havaalanlari.txt`'de GERÇEKTEN
    bulunuyor (bkz. rapor). 6 farklı saatte queue-producing event.
  - SMALL ölçek gerçek test airport'u: MFG (Muzaffarabad, OPMF,
    Asia/Karachi) - `data/kucuk_olcekli_havaalanlari.txt`'de GERÇEKTEN
    bulunuyor. 6 farklı saatte queue-producing event.
  - UNKNOWN ölçek gerçek test airport'u: OAG (Orange Airport, YORG,
    Australia/Sydney) - `flight_airports.sql`'de GERÇEK bir kayıt, ama
    ÜÇ ölçek dosyasının HİÇBİRİNDE yok (gerçek "unknown scale" senaryosu,
    uydurma bir kod DEĞİL). 3 farklı saatte queue-producing event.

Tüm yeni flight'lar TM9000+ flight number serisiyle işaretli - gerçek
200 flight'la VE `_generate_extra_test_flights.py`'nin TS9xxx serisiyle
ASLA çakışmaz.

Aircraft tipleri (A320/A20N/B738/B772/A388) `_generate_extra_test_
flights.py` ile AYNI - resolver'ın ZATEN tanıdığı gerçek ICAO tipler,
kapasite hiç hard-code edilmedi.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPLAY_DIR = Path(__file__).resolve().parent
REPO_ROOT = REPLAY_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.queue.domain.operational_day import (  # noqa: E402
    operational_day_window,
    resolve_airport_timezone,
)

DEP_PATH = REPLAY_DIR / "Delays - Type Departures.json"
ARR_PATH = REPLAY_DIR / "Delays - Type Arrivals.json"

MARKER_PREFIX = "TM9"


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path: Path, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _already_generated() -> bool:
    dep = _load(DEP_PATH)
    return any(
        (r.get("flight_iata") or "").startswith(MARKER_PREFIX)
        for r in dep["response"]
    )


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _record(
    *,
    num: int,
    dep_iata: str, dep_icao: str, dep_tz: ZoneInfo,
    arr_iata: str, arr_icao: str, arr_tz: ZoneInfo,
    dep_time_utc: datetime, arr_time_utc: datetime,
    aircraft_icao: str,
) -> dict:
    """Gerçek şema - `_generate_extra_test_flights.py:_record()` ile BİREBİR AYNI (delay yok, scheduled==estimated==actual)."""
    dep_local = dep_time_utc.astimezone(dep_tz) if dep_tz else dep_time_utc
    arr_local = arr_time_utc.astimezone(arr_tz) if arr_tz else arr_time_utc

    return {
        "airline_iata": "TM",
        "airline_icao": "TMX",
        "flight_iata": f"TM9{num:03d}",
        "flight_icao": f"TMX9{num:03d}",
        "flight_number": f"9{num:03d}",
        "dep_iata": dep_iata,
        "dep_icao": dep_icao,
        "dep_terminal": "T1",
        "dep_gate": None,
        "dep_time": _fmt(dep_local.replace(tzinfo=None)),
        "dep_time_utc": _fmt(dep_time_utc),
        "dep_estimated": _fmt(dep_local.replace(tzinfo=None)),
        "dep_estimated_utc": _fmt(dep_time_utc),
        "dep_actual": _fmt(dep_local.replace(tzinfo=None)),
        "dep_actual_utc": _fmt(dep_time_utc),
        "arr_iata": arr_iata,
        "arr_icao": arr_icao,
        "arr_terminal": None,
        "arr_gate": None,
        "arr_baggage": None,
        "arr_time": _fmt(arr_local.replace(tzinfo=None)),
        "arr_time_utc": _fmt(arr_time_utc),
        "arr_estimated": _fmt(arr_local.replace(tzinfo=None)),
        "arr_estimated_utc": _fmt(arr_time_utc),
        "arr_actual": _fmt(arr_local.replace(tzinfo=None)),
        "arr_actual_utc": _fmt(arr_time_utc),
        "cs_airline_iata": None,
        "cs_flight_number": None,
        "cs_flight_iata": None,
        "status": "landed",
        "duration": max(int((arr_time_utc - dep_time_utc).total_seconds() // 60), 30),
        "delayed": 0,
        "dep_delayed": 0,
        "arr_delayed": 0,
        "aircraft_icao": aircraft_icao,
        "arr_time_ts": int(arr_time_utc.timestamp()),
        "dep_time_ts": int(dep_time_utc.timestamp()),
        "arr_estimated_ts": int(arr_time_utc.timestamp()),
        "dep_estimated_ts": int(dep_time_utc.timestamp()),
        "arr_actual_ts": int(arr_time_utc.timestamp()),
        "dep_actual_ts": int(dep_time_utc.timestamp()),
    }


# --- Airport sabitleri (GERÇEK, flight_airports.sql'de olan kodlar) ---
CBR = ("CBR", "YSCB", "Australia/Sydney")     # MEDIUM (orta_olcekli...)
MFG = ("MFG", "OPMF", "Asia/Karachi")          # SMALL (kucuk_olcekli...)
OAG = ("OAG", "YORG", "Australia/Sydney")      # UNKNOWN (3 dosyada da YOK)

# Domestic partnerler (gerçek, aynı ülke)
CBR_DOM_PARTNER = ("SYD", "YSSY", "Australia/Sydney")
MFG_DOM_PARTNER = ("ISB", "OPIS", "Asia/Karachi")
OAG_DOM_PARTNER = ("SYD", "YSSY", "Australia/Sydney")

# Uluslararası partnerler (gerçek, ülke dışı)
CBR_INTL_PARTNER = ("LHR", "EGLL", "Europe/London")
MFG_INTL_PARTNER = ("DXB", "OMDB", "Asia/Dubai")
OAG_INTL_PARTNER = ("SIN", "WSSS", "Asia/Singapore")

AC_SMALL = ["A320", "A20N"]
AC_MED = ["B738", "A21N"]
AC_BIG = ["A388", "B772", "B744"]


def _six_hour_pattern(
    airport: tuple, dom_partner: tuple, intl_partner: tuple,
    anchor: datetime, start_num: int,
) -> tuple[list[dict], list[dict]]:
    """
    Phase 5/6 - 6 farklı saat (00/04/08/12/16/20), sakin/orta/yüksek/
    critical yoğunluk deseni, international departure + international
    arrival + domestic departure hepsi mevcut.

        00: international departure (sakin  - 1x A320)
        04: domestic departure      (orta    - 2x B738)
        08: international arrival   (sakin   - 1x A20N)
        12: international departure (yüksek  - 3x A388/B772/B744)
        16: domestic departure      (orta    - 2x A21N)
        20: international arrival   (critical - 3x A388/B772/B744)
    """
    iata, icao, tz_name = airport
    tz = resolve_airport_timezone(tz_name)
    window_start, _ = operational_day_window(tz, anchor)

    dom_iata, dom_icao, dom_tz_name = dom_partner
    dom_tz = resolve_airport_timezone(dom_tz_name)
    intl_iata, intl_icao, intl_tz_name = intl_partner
    intl_tz = resolve_airport_timezone(intl_tz_name)

    departures: list[dict] = []
    arrivals: list[dict] = []
    num = start_num

    plan = [
        (0, "intl_dep", [AC_SMALL[0]]),
        (4, "dom_dep", [AC_MED[0], AC_MED[1]]),
        (8, "intl_arr", [AC_SMALL[1]]),
        (12, "intl_dep", [AC_BIG[0], AC_BIG[1], AC_BIG[2]]),
        (16, "dom_dep", [AC_MED[1], AC_MED[0]]),
        (20, "intl_arr", [AC_BIG[1], AC_BIG[2], AC_BIG[0]]),
    ]

    for hour, kind, aircraft_list in plan:
        target = window_start + timedelta(hours=hour)
        for i, aircraft in enumerate(aircraft_list):
            num += 1
            jitter = timedelta(minutes=i * 3)
            if kind == "intl_dep":
                dep_actual = target + jitter
                arr_actual = dep_actual + timedelta(hours=6)
                departures.append(_record(
                    num=num,
                    dep_iata=iata, dep_icao=icao, dep_tz=tz,
                    arr_iata=intl_iata, arr_icao=intl_icao, arr_tz=intl_tz,
                    dep_time_utc=dep_actual, arr_time_utc=arr_actual,
                    aircraft_icao=aircraft,
                ))
            elif kind == "dom_dep":
                dep_actual = target + jitter
                arr_actual = dep_actual + timedelta(minutes=75)
                departures.append(_record(
                    num=num,
                    dep_iata=iata, dep_icao=icao, dep_tz=tz,
                    arr_iata=dom_iata, arr_icao=dom_icao, arr_tz=dom_tz,
                    dep_time_utc=dep_actual, arr_time_utc=arr_actual,
                    aircraft_icao=aircraft,
                ))
            else:  # intl_arr
                arr_actual = target + jitter
                dep_actual = arr_actual - timedelta(hours=6)
                arrivals.append(_record(
                    num=num,
                    dep_iata=intl_iata, dep_icao=intl_icao, dep_tz=intl_tz,
                    arr_iata=iata, arr_icao=icao, arr_tz=tz,
                    dep_time_utc=dep_actual, arr_time_utc=arr_actual,
                    aircraft_icao=aircraft,
                ))

    return departures, arrivals


def build_records() -> tuple[list[dict], list[dict]]:
    anchor = datetime(2026, 9, 18, 12, 0)

    departures: list[dict] = []
    arrivals: list[dict] = []
    num = 0

    # 1) CBR - MEDIUM, 6 saat.
    d, a = _six_hour_pattern(CBR, CBR_DOM_PARTNER, CBR_INTL_PARTNER, anchor, num)
    departures += d; arrivals += a
    num += len(d) + len(a)

    # 2) MFG - SMALL, 6 saat.
    d, a = _six_hour_pattern(MFG, MFG_DOM_PARTNER, MFG_INTL_PARTNER, anchor, num)
    departures += d; arrivals += a
    num += len(d) + len(a)

    # 3) OAG - UNKNOWN scale, SADECE 3 saat (08 intl dep, 14 dom dep, 20 intl arr).
    oag_tz = resolve_airport_timezone(OAG[2])
    oag_window_start, _ = operational_day_window(oag_tz, anchor)
    dom_iata, dom_icao, dom_tz_name = OAG_DOM_PARTNER
    dom_tz = resolve_airport_timezone(dom_tz_name)
    intl_iata, intl_icao, intl_tz_name = OAG_INTL_PARTNER
    intl_tz = resolve_airport_timezone(intl_tz_name)

    num += 1
    dep_actual = oag_window_start + timedelta(hours=8)
    arr_actual = dep_actual + timedelta(hours=6)
    departures.append(_record(
        num=num, dep_iata=OAG[0], dep_icao=OAG[1], dep_tz=oag_tz,
        arr_iata=intl_iata, arr_icao=intl_icao, arr_tz=intl_tz,
        dep_time_utc=dep_actual, arr_time_utc=arr_actual, aircraft_icao="A320",
    ))

    num += 1
    dep_actual = oag_window_start + timedelta(hours=14)
    arr_actual = dep_actual + timedelta(minutes=75)
    departures.append(_record(
        num=num, dep_iata=OAG[0], dep_icao=OAG[1], dep_tz=oag_tz,
        arr_iata=dom_iata, arr_icao=dom_icao, arr_tz=dom_tz,
        dep_time_utc=dep_actual, arr_time_utc=arr_actual, aircraft_icao="A20N",
    ))

    num += 1
    arr_actual = oag_window_start + timedelta(hours=20)
    dep_actual = arr_actual - timedelta(hours=6)
    arrivals.append(_record(
        num=num, dep_iata=intl_iata, dep_icao=intl_icao, dep_tz=intl_tz,
        arr_iata=OAG[0], arr_icao=OAG[1], arr_tz=oag_tz,
        dep_time_utc=dep_actual, arr_time_utc=arr_actual, aircraft_icao="B738",
    ))

    return departures, arrivals


def main() -> None:
    if _already_generated():
        print("Zaten üretilmiş (TM9xxx marker bulundu) - tekrar EKLENMEDİ (idempotent).")
        return

    dep_payload = _load(DEP_PATH)
    arr_payload = _load(ARR_PATH)

    new_departures, new_arrivals = build_records()

    dep_payload["response"].extend(new_departures)
    arr_payload["response"].extend(new_arrivals)

    _save(DEP_PATH, dep_payload)
    _save(ARR_PATH, arr_payload)

    print(f"Eklenen departure kayıtları: {len(new_departures)}")
    print(f"Eklenen arrival kayıtları: {len(new_arrivals)}")
    print(f"Toplam yeni flight: {len(new_departures) + len(new_arrivals)}")


if __name__ == "__main__":
    main()
