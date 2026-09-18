"""
ADIM (Date-Shift Replay Test Data Expansion) - bu betik, TEK SEFERLİK
(idempotent - flight_iata prefix'i kontrol ediyor, tekrar çalıştırılırsa
DUPLICATE eklemez) olarak `tests/date_shift_replay/Delays - Type
Departures.json` / `Delays - Type Arrivals.json` dosyalarına, GERÇEK
AirLabs şemasıyla (bkz. mevcut kayıtlar - alan adları/nesting/timestamp
biçimi AYNEN) yeni test flight'ları EKLER.

SADECE bu klasördeki (`tests/date_shift_replay/`) shifted KOPYA
dosyalarına yazar - gerçek `data/*.json` HİÇ AÇILMAZ/YAZILMAZ.

Amaç (kullanıcı talimatı):
  1) CDG (mevcut, large-scale, Europe/Paris) için kendi operasyonel
     gününün (`operational_day_window()` - production fonksiyonu ile
     HESAPLANMIŞ, hard-code EDİLMEMİŞ) TAM 24 saatinin HER birinde
     en az bir queue-producing event (`effective_time()` - production
     fonksiyonu - ile doğrulanabilir) oluşturmak.
  2) FRA (Frankfurt) - mevcut 78 airport'ta OLMAYAN, gerçek directory
     kaydı + gerçek large-scale dosya eşleşmesi olan YENİ bir test
     airport'u - birkaç flight ile (international departure/arrival +
     domestic departure) eklemek.

Tüm yeni flight'lar TS9000+ flight number serisiyle işaretli (bkz.
Bölüm 18) - gerçek 200 flight'la ASLA çakışmaz (flight_key: havayolu+
numara+tarih+airport+direction - TS ve 9000+ numara hiçbir gerçek
kayıtta yok).

Aircraft tipleri (A320/A20N/B738/B772/A388) mevcut DB'de ZATEN
`AircraftCapacity` tablosunda çözülen GERÇEK ICAO tipleridir (bkz.
rapor) - resolver'ın tanımadığı uydurma bir tip YOK, kapasite HİÇ
hard-code edilmedi, production `AircraftCapacityService` çözecek.
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

MARKER_PREFIX = "TS9"  # flight_iata TS9000.. - Bölüm 18 işaretleme


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
    """
    Gerçek şema (bkz. modül docstring'i) - TEK fark: delay YOK
    (scheduled == estimated == actual), test'in hedeflediği queue-event
    saatinin ELLE hesaplanmış olmasını basit/doğrulanabilir tutmak
    için (production `effective_time()` önceliği actual>estimated>
    scheduled - üçü de AYNI verilirse öncelik zinciri hâlâ AYNEN
    çalışır, sadece hangisinin seçildiği belirsizliği ORTADAN kalkar).
    """
    dep_local = dep_time_utc.astimezone(dep_tz) if dep_tz else dep_time_utc
    arr_local = arr_time_utc.astimezone(arr_tz) if arr_tz else arr_time_utc

    return {
        "airline_iata": "TS",
        "airline_icao": "TSX",
        "flight_iata": f"TS9{num:03d}",
        "flight_icao": f"TSX9{num:03d}",
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


# --- Airport sabitleri (GERÇEK, mevcut `flight_airports.sql`'de olan kodlar) ---
CDG = ("CDG", "LFPG", "Europe/Paris")
FRA = ("FRA", "EDDF", "Europe/Berlin")
MUC = ("MUC", "EDDM", "Europe/Berlin")  # FRA domestic partner (DE)

# CDG domestic (FR) partnerleri - rotasyon
FR_PARTNERS = [
    ("ORY", "LFPO"), ("NCE", "LFMN"), ("MRS", "LFML"),
    ("LYS", "LFLL"), ("TLS", "LFBO"), ("BOD", "LFBD"),
]
# Uluslararası partnerler (FR/DE DIŞI) - rotasyon
INTL_PARTNERS = [
    ("JFK", "KJFK", "America/New_York"),
    ("LHR", "EGLL", "Europe/London"),
    ("NRT", "RJAA", "Asia/Tokyo"),
]

# Bölüm 6 - resolver'ın ZATEN tanıdığı gerçek ICAO tipler (bkz. rapor:
# mevcut DB'de A20N/A21N/A320/B39M/B738/B772 zaten çözülüyor; A388/B744
# de AircraftCapacity tablosunda mevcut - CRITICAL yoğunluk için).
AC_SMALL = ["A320", "A20N"]
AC_MED = ["B738", "A21N"]
AC_BIG = ["A388", "B772", "B744"]


def build_records() -> tuple[list[dict], list[dict]]:
    utc_tz = ZoneInfo("UTC")
    cdg_tz = resolve_airport_timezone(CDG[2])
    fra_tz = resolve_airport_timezone(FRA[2])

    window_start, window_end = operational_day_window(
        cdg_tz, datetime(2026, 9, 18, 12, 0)
    )
    assert (window_end - window_start) == timedelta(hours=24)

    departures: list[dict] = []
    arrivals: list[dict] = []
    num = 0

    # ====================================================================
    # 1) CDG - operasyonel günün (window_start..window_end) HER saati
    #    için en az 1 queue-producing event (Bölüm 3/4/5).
    # ====================================================================
    for k in range(24):
        target = window_start + timedelta(hours=k)
        type_cycle = k % 3          # 0=intl departure,1=domestic departure,2=intl arrival
        intensity_cycle = k % 4      # yoğunluk deseni

        # Bölüm 3/11 - GÜVENLİ SINIR: bir DEPARTURE flight'ının
        # `flight_reference_time()`'ı (=dep_actual_utc, operational-day
        # SEÇİMİNDE kullanılan production alanı) `effective_time()`'dan
        # (=dep_actual_utc - 120dk) TAM 120dk SONRA gelir. `target`,
        # operasyonel günün SON 120 dakikasına (`window_end - 120dk`)
        # denk gelirse dep_actual_utc >= window_end olur ve production
        # `filter_flights_for_operational_day()` (yarı-açık [start,end)
        # aralığı) bu flight'ı SESSİZCE "bugünün dışı" sayıp ELER - bu
        # production'da bir BUG değil, GERÇEK/doğru davranış (bkz.
        # rapor); TEST VERİSİ bu sınıra çarpmasın diye o dar aralıkta
        # departure YERİNE arrival türetilir (arrival'ın referans
        # zamanı `target - 15dk`, aynı sınıra ASLA çarpmaz).
        if type_cycle in (0, 1) and target >= window_end - timedelta(hours=2):
            type_cycle = 2

        if intensity_cycle == 0:
            count, aircraft_list = 1, [AC_SMALL[k % len(AC_SMALL)]]
        elif intensity_cycle == 1:
            count, aircraft_list = 2, [AC_MED[k % len(AC_MED)]] * 2
        elif intensity_cycle == 2:
            count, aircraft_list = 1, [AC_SMALL[(k + 1) % len(AC_SMALL)]]
        else:
            count, aircraft_list = 3, [AC_BIG[i % len(AC_BIG)] for i in range(3)]

        for i in range(count):
            num += 1
            # Aynı hedef saate düşen birden fazla flight'ta aircraft'a
            # göre birkaç dakika kaydırma - GERÇEK feed'lerde de aynı
            # dakikaya iki flight nadiren denk gelir, ama AYNI demand
            # bucket'ına (60 dk pencere) düşmeye devam eder.
            jitter = timedelta(minutes=i * 3)

            if type_cycle == 0:  # international departure -> passport_departure @ target
                partner_iata, partner_icao, partner_tz_name = INTL_PARTNERS[k % len(INTL_PARTNERS)]
                dep_actual = target + timedelta(minutes=120) + jitter
                arr_actual = dep_actual + timedelta(hours=8)
                rec = _record(
                    num=num,
                    dep_iata=CDG[0], dep_icao=CDG[1], dep_tz=cdg_tz,
                    arr_iata=partner_iata, arr_icao=partner_icao,
                    arr_tz=resolve_airport_timezone(partner_tz_name),
                    dep_time_utc=dep_actual, arr_time_utc=arr_actual,
                    aircraft_icao=aircraft_list[i],
                )
                departures.append(rec)
            elif type_cycle == 1:  # domestic departure -> security_domestic @ target
                partner_iata, partner_icao = FR_PARTNERS[k % len(FR_PARTNERS)]
                dep_actual = target + timedelta(minutes=120) + jitter
                arr_actual = dep_actual + timedelta(hours=1, minutes=20)
                rec = _record(
                    num=num,
                    dep_iata=CDG[0], dep_icao=CDG[1], dep_tz=cdg_tz,
                    arr_iata=partner_iata, arr_icao=partner_icao, arr_tz=cdg_tz,
                    dep_time_utc=dep_actual, arr_time_utc=arr_actual,
                    aircraft_icao=aircraft_list[i],
                )
                departures.append(rec)
            else:  # international arrival -> passport_arrival @ target
                partner_iata, partner_icao, partner_tz_name = INTL_PARTNERS[(k + 1) % len(INTL_PARTNERS)]
                arr_actual = target - timedelta(minutes=15) + jitter
                dep_actual = arr_actual - timedelta(hours=8)
                rec = _record(
                    num=num,
                    dep_iata=partner_iata, dep_icao=partner_icao,
                    dep_tz=resolve_airport_timezone(partner_tz_name),
                    arr_iata=CDG[0], arr_icao=CDG[1], arr_tz=cdg_tz,
                    dep_time_utc=dep_actual, arr_time_utc=arr_actual,
                    aircraft_icao=aircraft_list[i],
                )
                arrivals.append(rec)

    # ====================================================================
    # 2) FRA - YENİ test airport'u (Bölüm 7) - mevcut 78 airport'ta YOK,
    #    gerçek directory kaydı (EDDF, Europe/Berlin, DE) + gerçek
    #    large-scale dosya eşleşmesi (bkz. rapor - `ensure_airport_
    #    scales()` zaten TÜM Airport satırlarını çözüyor, FRA dahil).
    # ====================================================================
    fra_now_anchor = datetime(2026, 9, 18, 12, 0)
    fra_window_start, _ = operational_day_window(fra_tz, fra_now_anchor)

    # a) international departure FRA -> LHR
    num += 1
    dep_actual = fra_window_start + timedelta(hours=8)
    arr_actual = dep_actual + timedelta(hours=2)
    departures.append(_record(
        num=num,
        dep_iata=FRA[0], dep_icao=FRA[1], dep_tz=fra_tz,
        arr_iata="LHR", arr_icao="EGLL", arr_tz=resolve_airport_timezone("Europe/London"),
        dep_time_utc=dep_actual, arr_time_utc=arr_actual,
        aircraft_icao="A320",
    ))

    # b) international arrival JFK -> FRA
    num += 1
    arr_actual = fra_window_start + timedelta(hours=10)
    dep_actual = arr_actual - timedelta(hours=8)
    arrivals.append(_record(
        num=num,
        dep_iata="JFK", dep_icao="KJFK", dep_tz=resolve_airport_timezone("America/New_York"),
        arr_iata=FRA[0], arr_icao=FRA[1], arr_tz=fra_tz,
        dep_time_utc=dep_actual, arr_time_utc=arr_actual,
        aircraft_icao="B772",
    ))

    # c) domestic departure FRA -> MUC
    num += 1
    dep_actual = fra_window_start + timedelta(hours=12)
    arr_actual = dep_actual + timedelta(minutes=45)
    departures.append(_record(
        num=num,
        dep_iata=FRA[0], dep_icao=FRA[1], dep_tz=fra_tz,
        arr_iata=MUC[0], arr_icao=MUC[1], arr_tz=fra_tz,
        dep_time_utc=dep_actual, arr_time_utc=arr_actual,
        aircraft_icao="A20N",
    ))

    return departures, arrivals


def main() -> None:
    if _already_generated():
        print("Zaten üretilmiş (TS9xxx marker bulundu) - tekrar EKLENMEDİ (idempotent).")
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
