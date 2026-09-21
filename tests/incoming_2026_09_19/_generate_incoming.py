"""
ADIM (New Daily Incoming Data - 2026-09-19) - "dış API'den 19 Eylül'e
ait YENİ veri gelmiş" senaryosunu temsil eden, GERÇEK AirLabs şemasıyla
(bkz. `data/Delays - Type *.json`, `data/response-delays.json` -
alan adları/nesting/timestamp biçimi AYNEN) üretilmiş bir "incoming
batch".

Bu betik SADECE bu klasöre (`tests/incoming_2026_09_19/`) yazar -
gerçek `data/*.json` ve `database.sqlite` HİÇ AÇILMAZ/YAZILMAZ. Mevcut
`tests/date_shift_replay/` fixture'larına da APPEND EDİLMEZ - bu,
BAĞIMSIZ bir "yeni gelen batch" olarak kalır (bkz. görev talimatı §2).

İçerik:
  - IST (large scale): 7 flight - 3 international departure (LHR/FRA/
    CDG - CDG'de 3 uçak ~15 dk'da bir "yoğun saat" kümesi) + 2 domestic
    departure (ADB/AYT).
  - SAW (large scale): 6 flight - 3 international departure (AMS x2/
    VIE/FRA) + 2 domestic departure (ADB/AYT).
  - Destination arrival kayıtları (Arrivals.json'a AYRI kayıt olarak,
    aynı flight'ın KENDİ havalimanı bakış açısından): LHR, CDG (IST
    kökenli), AMS, FRA (SAW kökenli) - 4 farklı GERÇEK, large-scale
    destination.
  - `flights_live.json` (Source B, ADS-B) - SADECE 1 flight için
    (IST->FRA) Kaynak A'nın kendi `aircraft_icao`'sı BİLİNÇLİ OLARAK
    boş bırakıldı; gerçek enrichment mekanizmasının (flight_iata/
    flight_icao + tarih eşleşmesi) bunu ÇÖZDÜĞÜNÜ kanıtlamak için.

Tüm aircraft tipleri (A320/A321/B738/B789) gerçek `AircraftCapacityService`
tarafından ZATEN çözülen (`verified_dataset`) tiplerdir - kapasite hiç
hard-code edilmedi.
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

# --- Gerçek airport sabitleri (flight_airports.sql'de doğrulandı) ---
IST = ("IST", "LTFM", "Europe/Istanbul")
SAW = ("SAW", "LTFJ", "Europe/Istanbul")
ADB = ("ADB", "LTBJ", "Europe/Istanbul")
AYT = ("AYT", "LTAI", "Europe/Istanbul")
LHR = ("LHR", "EGLL", "Europe/London")
FRA = ("FRA", "EDDF", "Europe/Berlin")
CDG = ("CDG", "LFPG", "Europe/Paris")
AMS = ("AMS", "EHAM", "Europe/Amsterdam")
VIE = ("VIE", "LOWW", "Europe/Vienna")
ZRH = ("ZRH", "LSZH", "Europe/Zurich")
MUC = ("MUC", "EDDM", "Europe/Berlin")
MXP = ("MXP", "LIMC", "Europe/Rome")
FCO = ("FCO", "LIRF", "Europe/Rome")
MAD = ("MAD", "LEMD", "Europe/Madrid")
BCN = ("BCN", "LEBL", "Europe/Madrid")
DXB = ("DXB", "OMDB", "Asia/Dubai")
DOH = ("DOH", "OTHH", "Asia/Qatar")
JED = ("JED", "OEJN", "Asia/Riyadh")
RUH = ("RUH", "OERK", "Asia/Riyadh")

OP_DATE = datetime(2026, 9, 19)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _record(
    *, airline_iata, airline_icao, flight_number,
    dep_iata, dep_icao, dep_tz, dep_local_time,
    arr_iata, arr_icao, arr_tz, duration_minutes,
    aircraft_icao,  # None ise Source A alanı BOŞ bırakılır (enrichment testi)
    dep_terminal="1", arr_terminal=None,
) -> dict:
    """
    Gerçek şema (bkz. modül docstring'i, `data/Delays - Type
    Departures.json` ile alan-alan AYNI) - actual==estimated==scheduled
    (delay yok) - queue event'in hangi saate düşeceği doğrudan ve
    tahmin edilebilir olsun diye (production actual>estimated>scheduled
    önceliği hâlâ AYNEN geçerli, sadece üçü aynı verildi).
    """
    dep_local = dep_local_time.replace(tzinfo=dep_tz)
    dep_utc = dep_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    arr_local_naive = dep_local_time + timedelta(minutes=duration_minutes)
    arr_local = arr_local_naive.replace(tzinfo=dep_tz).astimezone(arr_tz)
    arr_utc = arr_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    arr_local_naive_display = arr_local.replace(tzinfo=None)

    flight_iata = f"{airline_iata}{flight_number}"
    flight_icao_code = f"{airline_icao}{flight_number}"

    rec = {
        "airline_iata": airline_iata,
        "airline_icao": airline_icao,
        "flight_iata": flight_iata,
        "flight_icao": flight_icao_code,
        "flight_number": flight_number,
        "dep_iata": dep_iata,
        "dep_icao": dep_icao,
        "dep_terminal": dep_terminal,
        "dep_gate": None,
        "dep_time": _fmt(dep_local_time),
        "dep_time_utc": _fmt(dep_utc),
        "dep_estimated": _fmt(dep_local_time),
        "dep_estimated_utc": _fmt(dep_utc),
        "dep_actual": _fmt(dep_local_time),
        "dep_actual_utc": _fmt(dep_utc),
        "arr_iata": arr_iata,
        "arr_icao": arr_icao,
        "arr_terminal": arr_terminal,
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
    return rec


def _departure_only(rec: dict) -> dict:
    """Arrivals.json'a AYRI bir kayıt eklemeyeceğimiz flight'lar için departure kaydı olduğu gibi kalır."""
    return rec


def build() -> tuple[list[dict], list[dict], list[dict]]:
    departures: list[dict] = []
    arrivals: list[dict] = []
    live: list[dict] = []

    def tz(spec):
        return resolve_airport_timezone(spec[2])

    # ================================================================
    # IST (large) - 7 flight.
    # ================================================================
    ist_lhr = _record(
        airline_iata="TK", airline_icao="THY", flight_number="1983",
        dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST), dep_local_time=OP_DATE.replace(hour=6, minute=15),
        arr_iata=LHR[0], arr_icao=LHR[1], arr_tz=tz(LHR), duration_minutes=240,
        aircraft_icao="B789", dep_terminal="1", arr_terminal="5",
    )
    departures.append(ist_lhr)
    arrivals.append(_record(
        airline_iata="TK", airline_icao="THY", flight_number="1983",
        dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST), dep_local_time=OP_DATE.replace(hour=6, minute=15),
        arr_iata=LHR[0], arr_icao=LHR[1], arr_tz=tz(LHR), duration_minutes=240,
        aircraft_icao="B789", dep_terminal="1", arr_terminal="5",
    ))

    ist_fra = _record(
        airline_iata="TK", airline_icao="THY", flight_number="1731",
        dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST), dep_local_time=OP_DATE.replace(hour=10, minute=5),
        arr_iata=FRA[0], arr_icao=FRA[1], arr_tz=tz(FRA), duration_minutes=210,
        aircraft_icao=None,  # BİLİNÇLİ boş - flights_live.json enrichment'ı test eder (Bölüm 12).
        dep_terminal="1", arr_terminal="2",
    )
    departures.append(ist_fra)
    live.append({
        "hex": "4BA001", "reg_number": "TC-LGA", "flag": "TR",
        "lat": 45.0, "lng": 20.0, "alt": 11000, "dir": 310.0, "speed": 470,
        "v_speed": 0, "squawk": "1000",
        "flight_number": "1731", "flight_icao": "THY1731",
        "dep_icao": IST[1], "dep_iata": IST[0], "arr_icao": FRA[1], "arr_iata": FRA[0],
        "airline_icao": "THY", "aircraft_icao": "A321",
        "updated": int(datetime(2026, 9, 19, 8, 30, tzinfo=ZoneInfo("UTC")).timestamp()),
        "status": "en-route", "type": "adsb",
    })

    # CDG "yoğun saat" kümesi - 3 international departure ~15 dk içinde.
    cdg_times = [(12, 0), (12, 8), (12, 14)]
    cdg_aircraft = ["A320", "A321", "B738"]
    cdg_numbers = ["1957", "1959", "1961"]
    ist_cdg_first = None
    for (h, m), ac, num in zip(cdg_times, cdg_aircraft, cdg_numbers):
        rec = _record(
            airline_iata="TK", airline_icao="THY", flight_number=num,
            dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST), dep_local_time=OP_DATE.replace(hour=h, minute=m),
            arr_iata=CDG[0], arr_icao=CDG[1], arr_tz=tz(CDG), duration_minutes=225,
            aircraft_icao=ac, dep_terminal="1", arr_terminal="2E",
        )
        departures.append(rec)
        if ist_cdg_first is None:
            ist_cdg_first = rec

    arrivals.append(_record(
        airline_iata="TK", airline_icao="THY", flight_number=cdg_numbers[0],
        dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST), dep_local_time=OP_DATE.replace(hour=cdg_times[0][0], minute=cdg_times[0][1]),
        arr_iata=CDG[0], arr_icao=CDG[1], arr_tz=tz(CDG), duration_minutes=225,
        aircraft_icao=cdg_aircraft[0], dep_terminal="1", arr_terminal="2E",
    ))

    # Domestic departure - sakin/orta saat.
    departures.append(_record(
        airline_iata="TK", airline_icao="THY", flight_number="2011",
        dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST), dep_local_time=OP_DATE.replace(hour=8, minute=10),
        arr_iata=ADB[0], arr_icao=ADB[1], arr_tz=tz(ADB), duration_minutes=75,
        aircraft_icao="A320", dep_terminal="1", arr_terminal=None,
    ))
    departures.append(_record(
        airline_iata="TK", airline_icao="THY", flight_number="2151",
        dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST), dep_local_time=OP_DATE.replace(hour=18, minute=20),
        arr_iata=AYT[0], arr_icao=AYT[1], arr_tz=tz(AYT), duration_minutes=70,
        aircraft_icao="A321", dep_terminal="1", arr_terminal=None,
    ))

    # ================================================================
    # SAW (large) - 6 flight.
    # ================================================================
    departures.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="871",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=6, minute=40),
        arr_iata=AMS[0], arr_icao=AMS[1], arr_tz=tz(AMS), duration_minutes=225,
        aircraft_icao="A321", dep_terminal="I", arr_terminal="3",
    ))
    arrivals.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="871",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=6, minute=40),
        arr_iata=AMS[0], arr_icao=AMS[1], arr_tz=tz(AMS), duration_minutes=225,
        aircraft_icao="A321", dep_terminal="I", arr_terminal="3",
    ))

    departures.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="915",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=10, minute=30),
        arr_iata=VIE[0], arr_icao=VIE[1], arr_tz=tz(VIE), duration_minutes=195,
        aircraft_icao="A320", dep_terminal="I", arr_terminal=None,
    ))

    departures.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="683",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=15, minute=15),
        arr_iata=FRA[0], arr_icao=FRA[1], arr_tz=tz(FRA), duration_minutes=210,
        aircraft_icao="B738", dep_terminal="I", arr_terminal="1",
    ))
    arrivals.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="683",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=15, minute=15),
        arr_iata=FRA[0], arr_icao=FRA[1], arr_tz=tz(FRA), duration_minutes=210,
        aircraft_icao="B738", dep_terminal="I", arr_terminal="1",
    ))

    departures.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="1203",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=8, minute=30),
        arr_iata=ADB[0], arr_icao=ADB[1], arr_tz=tz(ADB), duration_minutes=75,
        aircraft_icao="A320", dep_terminal="I", arr_terminal=None,
    ))
    departures.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="1311",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=12, minute=20),
        arr_iata=AYT[0], arr_icao=AYT[1], arr_tz=tz(AYT), duration_minutes=70,
        aircraft_icao="A321", dep_terminal="I", arr_terminal=None,
    ))
    departures.append(_record(
        airline_iata="PC", airline_icao="PGT", flight_number="879",
        dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW), dep_local_time=OP_DATE.replace(hour=21, minute=10),
        arr_iata=AMS[0], arr_icao=AMS[1], arr_tz=tz(AMS), duration_minutes=225,
        aircraft_icao="A320", dep_terminal="I", arr_terminal="3",
    ))

    # ================================================================
    # ADIM (24-Hour Graph + Realistic IST/SAW Density) - Bölüm N-W:
    # IST/SAW için GÜN BOYU gerçekçi dağılım (sakin gece, 05-08/10-13/
    # 16-20 peak bank'ları) - SADECE bu TEST/REPLAY fixture'ı için,
    # gerçek `data/*` dosyaları HİÇ DOKUNULMADI. Risk/wait HİÇ YAZILMADI
    # (Bölüm Y) - sadece INPUT flight kayıtları; OUTPUT tamamen gerçek
    # `run_predictions()`/event-driven motor tarafından hesaplanır.
    #
    # Her hücre: (hour, n_intl_dep, n_intl_arr, n_dom_dep, n_dom_arr).
    # 06/08/10/12/18 saatleri YUKARIDAKİ orijinal (TS/PC ile numaralanmış,
    # regresyon testlerinin bağımlı olduğu) flight'ları ZATEN içeriyor -
    # buradaki profil SADECE EK/tamamlayıcı yoğunluktur, üst üste binmeyi
    # (aynı dakika) önlemek için farklı dakikalar kullanılır.
    IST_HOURLY_PROFILE = [
        (0, 0, 0, 1, 0), (2, 1, 0, 0, 0),
        (4, 0, 0, 0, 1), (5, 1, 0, 1, 0),
        (6, 1, 0, 0, 0),                       # 06:15 TK1983 (LHR) zaten var -> peak bank'a katkı
        (7, 0, 1, 1, 0),
        (8, 0, 1, 0, 1),                       # 08:10 TK2011 (ADB dom dep) zaten var
        (9, 1, 0, 0, 0),
        (10, 0, 1, 0, 0),                      # 10:05 TK1731 (FRA) zaten var
        (11, 0, 1, 1, 0),
        (12, 0, 1, 0, 1),                      # 12:00/08/14 CDG kümesi zaten var
        (13, 1, 0, 0, 0), (14, 0, 1, 1, 0), (15, 0, 0, 0, 1),
        (16, 1, 0, 1, 0), (17, 0, 1, 0, 0),
        (18, 1, 0, 0, 0),                      # 18:20 TK2151 (AYT dom dep) zaten var
        (19, 1, 0, 1, 0), (20, 0, 1, 0, 0),
        (21, 1, 0, 0, 0), (22, 0, 1, 0, 0), (23, 0, 0, 1, 0),
    ]
    IST_INTL_PARTNERS = [LHR, FRA, CDG, AMS, VIE]
    IST_DOM_PARTNERS = [ADB, AYT]
    IST_INTL_AIRCRAFT = ["A320", "A321", "B738", "A333", "B789"]

    filler_num = 3000
    for hour, n_dep, n_arr, n_dom_dep, n_dom_arr in IST_HOURLY_PROFILE:
        for i in range(n_dep):
            filler_num += 1
            partner = IST_INTL_PARTNERS[filler_num % len(IST_INTL_PARTNERS)]
            aircraft = IST_INTL_AIRCRAFT[filler_num % len(IST_INTL_AIRCRAFT)]
            minute = 35 + i * 7
            departures.append(_record(
                airline_iata="TK", airline_icao="THY", flight_number=str(filler_num),
                dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST),
                dep_local_time=OP_DATE.replace(hour=hour, minute=minute),
                arr_iata=partner[0], arr_icao=partner[1], arr_tz=tz(partner),
                duration_minutes=210 if aircraft in ("A333", "B789") else 195,
                aircraft_icao=aircraft, dep_terminal="1", arr_terminal=None,
            ))
        for i in range(n_arr):
            filler_num += 1
            partner = IST_INTL_PARTNERS[filler_num % len(IST_INTL_PARTNERS)]
            aircraft = IST_INTL_AIRCRAFT[filler_num % len(IST_INTL_AIRCRAFT)]
            minute = 40 + i * 7
            duration = 210 if aircraft in ("A333", "B789") else 195
            arr_local = OP_DATE.replace(hour=hour, minute=minute)
            arrivals.append(_record(
                airline_iata="TK", airline_icao="THY", flight_number=str(filler_num),
                dep_iata=partner[0], dep_icao=partner[1], dep_tz=tz(partner),
                dep_local_time=arr_local - timedelta(minutes=duration),
                arr_iata=IST[0], arr_icao=IST[1], arr_tz=tz(IST),
                duration_minutes=duration,
                aircraft_icao=aircraft, dep_terminal=None, arr_terminal="1",
            ))
        for i in range(n_dom_dep):
            filler_num += 1
            partner = IST_DOM_PARTNERS[filler_num % len(IST_DOM_PARTNERS)]
            minute = 15 + i * 6
            departures.append(_record(
                airline_iata="TK", airline_icao="THY", flight_number=str(filler_num),
                dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST),
                dep_local_time=OP_DATE.replace(hour=hour, minute=minute),
                arr_iata=partner[0], arr_icao=partner[1], arr_tz=tz(partner),
                duration_minutes=75, aircraft_icao="A320", dep_terminal="1", arr_terminal=None,
            ))
        for i in range(n_dom_arr):
            # Bölüm S - domestic ARRIVAL: production contract'ına göre
            # HİÇ queue üretmemeli (invariant testi).
            filler_num += 1
            partner = IST_DOM_PARTNERS[filler_num % len(IST_DOM_PARTNERS)]
            minute = 50
            arr_local = OP_DATE.replace(hour=hour, minute=minute)
            arrivals.append(_record(
                airline_iata="TK", airline_icao="THY", flight_number=str(filler_num),
                dep_iata=partner[0], dep_icao=partner[1], dep_tz=tz(partner),
                dep_local_time=arr_local - timedelta(minutes=75),
                arr_iata=IST[0], arr_icao=IST[1], arr_tz=tz(IST),
                duration_minutes=75, aircraft_icao="A321", dep_terminal=None, arr_terminal="1",
            ))

    # SAW - daha küçük ama karışık bir ek örneklem (Bölüm T).
    SAW_HOURLY_PROFILE = [
        (1, 1, 0, 0, 0), (3, 0, 0, 1, 0), (9, 1, 1, 0, 0),
        (11, 0, 1, 1, 0), (13, 1, 0, 0, 1), (14, 0, 1, 0, 0),
        (17, 1, 0, 1, 0), (19, 0, 1, 0, 0), (23, 0, 0, 1, 0),
    ]
    SAW_INTL_PARTNERS = [AMS, VIE, FRA]
    SAW_DOM_PARTNERS = [ADB, AYT]
    SAW_AIRCRAFT = ["A320", "A321", "B738"]

    saw_num = 5000
    for hour, n_dep, n_arr, n_dom_dep, n_dom_arr in SAW_HOURLY_PROFILE:
        for i in range(n_dep):
            saw_num += 1
            partner = SAW_INTL_PARTNERS[saw_num % len(SAW_INTL_PARTNERS)]
            aircraft = SAW_AIRCRAFT[saw_num % len(SAW_AIRCRAFT)]
            departures.append(_record(
                airline_iata="PC", airline_icao="PGT", flight_number=str(saw_num),
                dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW),
                dep_local_time=OP_DATE.replace(hour=hour, minute=25),
                arr_iata=partner[0], arr_icao=partner[1], arr_tz=tz(partner),
                duration_minutes=200, aircraft_icao=aircraft, dep_terminal="I", arr_terminal=None,
            ))
        for i in range(n_arr):
            saw_num += 1
            partner = SAW_INTL_PARTNERS[saw_num % len(SAW_INTL_PARTNERS)]
            aircraft = SAW_AIRCRAFT[saw_num % len(SAW_AIRCRAFT)]
            duration = 200
            arr_local = OP_DATE.replace(hour=hour, minute=30)
            arrivals.append(_record(
                airline_iata="PC", airline_icao="PGT", flight_number=str(saw_num),
                dep_iata=partner[0], dep_icao=partner[1], dep_tz=tz(partner),
                dep_local_time=arr_local - timedelta(minutes=duration),
                arr_iata=SAW[0], arr_icao=SAW[1], arr_tz=tz(SAW),
                duration_minutes=duration, aircraft_icao=aircraft, dep_terminal=None, arr_terminal="I",
            ))
        for i in range(n_dom_dep):
            saw_num += 1
            partner = SAW_DOM_PARTNERS[saw_num % len(SAW_DOM_PARTNERS)]
            departures.append(_record(
                airline_iata="PC", airline_icao="PGT", flight_number=str(saw_num),
                dep_iata=SAW[0], dep_icao=SAW[1], dep_tz=tz(SAW),
                dep_local_time=OP_DATE.replace(hour=hour, minute=35),
                arr_iata=partner[0], arr_icao=partner[1], arr_tz=tz(partner),
                duration_minutes=75, aircraft_icao="A320", dep_terminal="I", arr_terminal=None,
            ))
        for i in range(n_dom_arr):
            saw_num += 1
            partner = SAW_DOM_PARTNERS[saw_num % len(SAW_DOM_PARTNERS)]
            arr_local = OP_DATE.replace(hour=hour, minute=45)
            arrivals.append(_record(
                airline_iata="PC", airline_icao="PGT", flight_number=str(saw_num),
                dep_iata=partner[0], dep_icao=partner[1], dep_tz=tz(partner),
                dep_local_time=arr_local - timedelta(minutes=75),
                arr_iata=SAW[0], arr_icao=SAW[1], arr_tz=tz(SAW),
                duration_minutes=75, aircraft_icao="A321", dep_terminal=None, arr_terminal="I",
            ))

    # ================================================================
    # ADIM (IST International Departure High-Density) - Bölüm 1-4:
    # IST'nin uluslararası kalkış SAYISINI belirgin şekilde artırıp iki
    # AÇIK peak bank oluşturur (06:00-07:00 ve 17:00-19:00). Risk/wait
    # HİÇ yazılmadı (Bölüm Y) - sadece INPUT flight; production
    # `AircraftCapacityService`/event-driven motor OUTPUT'u üretir.
    # ================================================================
    IST_WIDE_INTL_PARTNERS = [ZRH, MUC, MXP, FCO, MAD, BCN, DXB, DOH, JED, RUH]
    IST_WIDE_SHORT_AIRCRAFT = ["A320", "A321", "B738"]
    IST_WIDE_LONG_AIRCRAFT = ["A333", "B789"]
    # DXB/DOH/JED/RUH (Orta Doğu) gerçekçi olarak uzun-menzil tipiyle,
    # Avrupa destinasyonları kısa/orta menzil tipiyle eşleştirilir.
    IST_WIDE_LONG_HAUL_CODES = {"DXB", "DOH", "JED", "RUH"}

    dense_num = 6000

    # Peak bank 1: 06:00-07:00, 6 flight, ~10 dk aralıklarla.
    bank1_minutes = [0, 10, 20, 30, 40, 50]
    for i, minute in enumerate(bank1_minutes):
        dense_num += 1
        partner = IST_WIDE_INTL_PARTNERS[dense_num % len(IST_WIDE_INTL_PARTNERS)]
        long_haul = partner[0] in IST_WIDE_LONG_HAUL_CODES
        aircraft = (
            IST_WIDE_LONG_AIRCRAFT[dense_num % len(IST_WIDE_LONG_AIRCRAFT)] if long_haul
            else IST_WIDE_SHORT_AIRCRAFT[dense_num % len(IST_WIDE_SHORT_AIRCRAFT)]
        )
        departures.append(_record(
            airline_iata="TK", airline_icao="THY", flight_number=str(dense_num),
            dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST),
            dep_local_time=OP_DATE.replace(hour=6, minute=minute),
            arr_iata=partner[0], arr_icao=partner[1], arr_tz=tz(partner),
            duration_minutes=300 if long_haul else 195,
            aircraft_icao=aircraft, dep_terminal="1", arr_terminal=None,
        ))

    # Peak bank 2: 17:00-19:00, 8 flight, ~15 dk aralıklarla.
    bank2_slots = [(17, 5), (17, 20), (17, 40), (17, 55), (18, 10), (18, 25), (18, 50), (19, 5)]
    for hour, minute in bank2_slots:
        dense_num += 1
        partner = IST_WIDE_INTL_PARTNERS[dense_num % len(IST_WIDE_INTL_PARTNERS)]
        long_haul = partner[0] in IST_WIDE_LONG_HAUL_CODES
        aircraft = (
            IST_WIDE_LONG_AIRCRAFT[dense_num % len(IST_WIDE_LONG_AIRCRAFT)] if long_haul
            else IST_WIDE_SHORT_AIRCRAFT[dense_num % len(IST_WIDE_SHORT_AIRCRAFT)]
        )
        departures.append(_record(
            airline_iata="TK", airline_icao="THY", flight_number=str(dense_num),
            dep_iata=IST[0], dep_icao=IST[1], dep_tz=tz(IST),
            dep_local_time=OP_DATE.replace(hour=hour, minute=minute),
            arr_iata=partner[0], arr_icao=partner[1], arr_tz=tz(partner),
            duration_minutes=300 if long_haul else 195,
            aircraft_icao=aircraft, dep_terminal="1", arr_terminal=None,
        ))

    return departures, arrivals, live


def main() -> None:
    departures, arrivals, live = build()

    dep_payload = {
        "request": {"host": "airlabs.co", "method": "schedules", "has_more": False},
        "response": departures,
        "terms": "https://airlabs.co/docs/terms (test fixture - production terms metni KOPYALANMADI, kaynak referansı olarak tutulur)",
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
