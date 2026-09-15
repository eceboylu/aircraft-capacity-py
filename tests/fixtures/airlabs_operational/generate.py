"""
ADIM 6A - Operasyonel T0/T1/T2 fixture üreticisi.

Bu script bir PYTEST DOSYASI DEĞİLDİR (bilinçli olarak `test_`
öneki YOK, `tests/stress_large_volume.py` ile aynı konvansiyon).
Çalıştırıldığında bu klasördeki (`t0/`, `t1/`, `t2/`) JSON dosyalarını
YENİDEN üretir - fixture'ların KENDİSİ commit edilir, script sadece
şeffaflık/tekrar-üretilebilirlik için burada tutulur.

ÇOK ÖNEMLİ - bu script hiçbir prediction/risk/yoğunluk DEĞERİ
üretmez. Sadece "uçuş operasyonu" üretir: hangi uçuş, hangi
scheduled/estimated/actual zamanda, hangi aircraft ile, hangi
statüde. Gerçek AirLabs `schedules` alan şemasını kullanır (bkz.
`tests/fixtures/airlabs_mock/README.md`). Security/passport/Erlang-C
sonuçları GERÇEK production motoru tarafından, bu veriler üzerinde,
gerçek pipeline zincirinden geçirilerek hesaplanır (bkz.
`tests/airlabs_operational_source.py` + `tests/test_operational_dataset.py`).

Senaryo tasarımı (ADIM 6A promptundaki bölüm numaralarıyla):

  T0 (§5)  - IST/SAW/ADB için normal, güne dengeli dağılmış tarife.
  T1 (§6-10) - IST'de:
      §6  security surge  : 08:00/08:15/08:30 efektif pencerelerine
                             hedeflenen 14 YENİ departure (dep_scheduled
                             geriye, o havalimanının GERÇEK buffer
                             kuralına göre hesaplanarak seçildi - bkz.
                             `_dep_scheduled_for_effective_window`).
      §7  passport surge  : 10:00-10:45 efektif pencerelerine hedeflenen
                             12 YENİ uluslararası arrival (aynı mantık,
                             passport'un SABİT 15 dk buffer'ı ile).
      §8  delay compression: TK101-104 (T0'da 4 farklı pencereye
                             düşen mevcut uçuşlar) - T1'de
                             dep_estimated_utc'leri 09:00 penceresine
                             ortaklaşacak şekilde değiştirildi.
      §9  aircraft change  : TK201/TK202 (T0'da A320) - T1'de B772.
      §10 cancellation     : surge bank'ındaki 3 uçuş T1'de cancelled.
      SAW'da orta seviyeli, daha küçük bir departure artışı.
      ADB'de HİÇBİR değişiklik - kontrol grubu.
  T2 (§11) - IST'de surge bank'ları (departure + passport) GENİŞ bir
             aralığa yayılarak "actual" zamanlar eklenir (dağılma/
             recovery); TK101-104 "actual"a kavuşur; cancelled uçuşlar
             cancelled kalır; yeni bir surge EKLENMEZ. SAW'da hafif
             yerleşme. ADB değişmez.

Hiçbir round'da flight_key'i (airline+number+SCHEDULED tarih) T0'dan
FARKLI kılacak bir alan (dep_scheduled_utc/arr_scheduled_utc) round'lar
arası DEĞİŞTİRİLMEZ - production'ın "flight_key sabit kalır" kuralı
bilinçli olarak korunuyor (bkz. app/queue/ingestion/sources.py
build_flight_key).
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta

OUT_DIR = os.path.dirname(__file__)
REF_DAY = "2026-09-15"

NARROW = ["A319", "A320", "A321", "B738", "B739"]
WIDE_MEDIUM = ["A330", "A359"]      # medium/long-haul widebody, buffer 60dk (duration 120-360)
WIDE_LONG = ["B772", "B788"]        # >360dk uçuşlarda kullanılacak, buffer 90dk

# Rota kategorisi -> (arr_iata havuzu, uçuş süresi dk, security buffer dk)
# Buffer değerleri PRODUCTION kuralından (app/queue/constants.py) alınmıştır,
# burada TEKRAR TANIMLANMAZ - sadece dep_scheduled'ı doğru hedefe
# oturtmak için kullanılır (bkz. RANGE/BUFFER sabitleri).
DOMESTIC_DESTS = ["ESB", "AYT", "ADA", "TZX"]          # TR-TR, kısa mesafe
INTL_MEDIUM_DESTS = ["LHR", "CDG", "FRA", "AMS", "DXB"]  # orta menzil
INTL_LONG_DESTS = ["JFK"]                                # uzun menzil

DURATION_DOMESTIC = 75      # dk, <=120 -> buffer 45
DURATION_INTL_MEDIUM = 240  # dk, 120<..<=360 -> buffer 60
DURATION_INTL_LONG = 600    # dk, >360 -> buffer 90

BUFFER_DOMESTIC = 45
BUFFER_INTL_MEDIUM = 60
BUFFER_INTL_LONG = 90
PASSPORT_BUFFER = 15        # sabit, tüm arrival'larda aynı


def _dt(hhmm: str) -> datetime:
    return datetime.strptime(f"{REF_DAY} {hhmm}", "%Y-%m-%d %H:%M")


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _dep_scheduled_for_effective_window(effective_hhmm: str, buffer_minutes: int) -> datetime:
    """
    Security effective_time = dep - buffer. İstenen efektif pencereye
    GERÇEK buffer kuralına göre denk gelecek dep_scheduled'ı üretir.
    """
    return _dt(effective_hhmm) + timedelta(minutes=buffer_minutes)


def _dep_scheduled_for_passport_window(effective_hhmm: str) -> datetime:
    """Passport effective_time = arr + 15dk (sabit) -> arr_scheduled = efektif - 15dk."""
    return _dt(effective_hhmm) - timedelta(minutes=PASSPORT_BUFFER)


def record(
    *,
    airline_iata,
    airline_icao,
    flight_number,
    dep_iata,
    arr_iata,
    dep_scheduled: datetime,
    duration_minutes: int,
    aircraft_icao,
    status="scheduled",
    dep_estimated: datetime | None = None,
    dep_actual: datetime | None = None,
    arr_estimated: datetime | None = None,
    arr_actual: datetime | None = None,
    dep_terminal="1",
    dep_gate="B1",
    arr_terminal="1",
    arr_gate="C1",
) -> dict:
    """Gerçek AirLabs `schedules` alan şemasıyla tek bir ham kayıt üretir."""
    arr_scheduled = dep_scheduled + timedelta(minutes=duration_minutes)
    return {
        "airline_iata": airline_iata,
        "airline_icao": airline_icao,
        "flight_iata": f"{airline_iata}{flight_number}",
        "flight_icao": f"{airline_icao}{flight_number}",
        "flight_number": flight_number,
        "dep_iata": dep_iata,
        "dep_icao": dep_iata,
        "dep_terminal": dep_terminal,
        "dep_gate": dep_gate,
        "dep_time": _fmt(dep_scheduled),
        "dep_time_utc": _fmt(dep_scheduled),
        "dep_estimated": _fmt(dep_estimated) if dep_estimated else None,
        "dep_estimated_utc": _fmt(dep_estimated) if dep_estimated else None,
        "dep_actual": _fmt(dep_actual) if dep_actual else None,
        "dep_actual_utc": _fmt(dep_actual) if dep_actual else None,
        "arr_iata": arr_iata,
        "arr_icao": arr_iata,
        "arr_terminal": arr_terminal,
        "arr_gate": arr_gate,
        "arr_baggage": None,
        "arr_time": _fmt(arr_scheduled),
        "arr_time_utc": _fmt(arr_scheduled),
        "arr_estimated": _fmt(arr_estimated) if arr_estimated else None,
        "arr_estimated_utc": _fmt(arr_estimated) if arr_estimated else None,
        "arr_actual": _fmt(arr_actual) if arr_actual else None,
        "arr_actual_utc": _fmt(arr_actual) if arr_actual else None,
        "duration": duration_minutes,
        "status": status,
        "aircraft_icao": aircraft_icao,
    }


def _route(index: int):
    """Deterministik rota rotasyonu: ~%40 domestic, %45 orta menzil, %15 uzun menzil."""
    bucket = index % 20
    if bucket < 8:
        return DOMESTIC_DESTS[index % len(DOMESTIC_DESTS)], DURATION_DOMESTIC, BUFFER_DOMESTIC, NARROW
    if bucket < 17:
        pool = NARROW if bucket % 3 else WIDE_MEDIUM
        return INTL_MEDIUM_DESTS[index % len(INTL_MEDIUM_DESTS)], DURATION_INTL_MEDIUM, BUFFER_INTL_MEDIUM, pool
    return INTL_LONG_DESTS[0], DURATION_INTL_LONG, BUFFER_INTL_LONG, WIDE_LONG


def baseline_schedule(
    airport: str, direction: str, count: int, airline_iata="TK", airline_icao="THY", start_number=100,
    start_hour=6, end_hour=22,
) -> list[dict]:
    """
    Güne dengeli dağılmış T0 tarifesi. `direction`="departure" ise
    `airport` kalkış, ="arrival" ise varış.
    """
    span_minutes = (end_hour - start_hour) * 60
    step = span_minutes / count
    out = []
    for i in range(count):
        dest, duration, _buffer, aircraft_pool = _route(i)
        aircraft = aircraft_pool[i % len(aircraft_pool)]
        minute_offset = int(i * step)
        base_time = _dt(f"{start_hour:02d}:00") + timedelta(minutes=minute_offset)

        if direction == "departure":
            dep_iata, arr_iata = airport, dest
            dep_scheduled = base_time
        else:
            dep_iata, arr_iata = dest, airport
            # Varış tarifesinde base_time'ı VARIŞ saati olarak kullanıyoruz,
            # kalkışı geriye (süre kadar) hesaplıyoruz.
            dep_scheduled = base_time - timedelta(minutes=duration)

        out.append(record(
            airline_iata=airline_iata,
            airline_icao=airline_icao,
            flight_number=str(start_number + i),
            dep_iata=dep_iata,
            arr_iata=arr_iata,
            dep_scheduled=dep_scheduled,
            duration_minutes=duration,
            aircraft_icao=aircraft,
        ))
    return out


def find(records: list[dict], flight_iata: str) -> dict:
    for r in records:
        if r["flight_iata"] == flight_iata:
            return r
    raise KeyError(flight_iata)


def build_ist():
    """
    IST - ana stres havalimanı. T0: ~90 departure + ~90 arrival = 180.
    """
    # start_number 1000/5000 - TK101-104 (delay), TK201-202 (capacity),
    # TK300-313 (security surge), TK400-411 (passport surge) blokları
    # ile ÇAKIŞMAYACAK şekilde bilinçli olarak ayrı bir aralık.
    t0_dep = baseline_schedule("IST", "departure", 90, start_number=1000)
    t0_arr = baseline_schedule("IST", "arrival", 90, start_number=5000)

    # --- §8 Delay compression'a hedef 4 uçuş: T0 tarifesine EK olarak
    # (baseline rotasyonuyla çakışmasın diye ayrı flight number
    # bloğunda) görev metnindeki örnek saatlerle (08:05/08:18/08:37/
    # 08:52) BİLİNÇLİ olarak farklı pencerelere düşecek domestic kısa
    # mesafeli uçuşlar ekleniyor.
    delay_targets = []
    for idx, hhmm in enumerate(["08:05", "08:18", "08:37", "08:52"]):
        r = record(
            airline_iata="TK", airline_icao="THY", flight_number=f"10{idx + 1}",
            dep_iata="IST", arr_iata=DOMESTIC_DESTS[idx % len(DOMESTIC_DESTS)],
            dep_scheduled=_dt(hhmm), duration_minutes=DURATION_DOMESTIC,
            aircraft_icao="A320",
        )
        delay_targets.append(r)
    t0_dep = delay_targets + t0_dep

    # --- §9 Aircraft change hedefi: TK201/TK202, T0'da A320, normal
    # saatlerde (surge pencerelerinden uzak, 14:00/15:00).
    capacity_targets = [
        record(
            airline_iata="TK", airline_icao="THY", flight_number="201",
            dep_iata="IST", arr_iata="CDG",
            dep_scheduled=_dt("14:00"), duration_minutes=DURATION_INTL_MEDIUM,
            aircraft_icao="A320",
        ),
        record(
            airline_iata="TK", airline_icao="THY", flight_number="202",
            dep_iata="IST", arr_iata="LHR",
            dep_scheduled=_dt("15:00"), duration_minutes=DURATION_INTL_MEDIUM,
            aircraft_icao="A320",
        ),
    ]
    t0_dep = t0_dep + capacity_targets

    # --- ADIM 6C §D - passport/arrival tarafı için ayrı hedef uçuşlar
    # (departure tarafındaki TK101-104/TK201-202 ile AYNI desen, ama
    # arrival + passport'a özel):
    #   TK601/TK602 : arrival delay compression hedefi - T0'da FARKLI
    #                 pencerelere düşer (09:30 / 10:00 efektif).
    #   TK701       : arrival aircraft capacity change hedefi - T0'da
    #                 narrowbody, normal saatte (surge'den uzak, 13:00).
    arrival_delay_targets = [
        record(
            airline_iata="TK", airline_icao="THY", flight_number="601",
            dep_iata="FRA", arr_iata="IST",
            dep_scheduled=_dt("09:20") - timedelta(minutes=DURATION_INTL_MEDIUM),
            duration_minutes=DURATION_INTL_MEDIUM, aircraft_icao="A321",
        ),
        record(
            airline_iata="TK", airline_icao="THY", flight_number="602",
            dep_iata="AMS", arr_iata="IST",
            dep_scheduled=_dt("09:50") - timedelta(minutes=DURATION_INTL_MEDIUM),
            duration_minutes=DURATION_INTL_MEDIUM, aircraft_icao="A320",
        ),
    ]
    arrival_capacity_target = record(
        airline_iata="TK", airline_icao="THY", flight_number="701",
        dep_iata="FRA", arr_iata="IST",
        dep_scheduled=_dt("13:00") - timedelta(minutes=DURATION_INTL_MEDIUM),
        duration_minutes=DURATION_INTL_MEDIUM, aircraft_icao="A320",
    )
    t0_arr = t0_arr + arrival_delay_targets + [arrival_capacity_target]

    t0 = {"departure": t0_dep, "arrival": t0_arr}

    # ================= T1 =================
    t1_dep = copy.deepcopy(t0["departure"])
    t1_arr = copy.deepcopy(t0["arrival"])

    # §6 Security surge - 08:00/08:15/08:30 efektif pencerelerine 14
    # YENİ departure (domestic kısa + widebody orta/uzun menzil).
    surge_dep = []
    surge_plan = [
        # (effective_window, dest, duration, buffer, aircraft)
        ("08:00", "ESB", DURATION_DOMESTIC, BUFFER_DOMESTIC, "A319"),
        ("08:00", "AYT", DURATION_DOMESTIC, BUFFER_DOMESTIC, "A321"),
        ("08:00", "ADA", DURATION_DOMESTIC, BUFFER_DOMESTIC, "B738"),
        ("08:00", "CDG", DURATION_INTL_MEDIUM, BUFFER_INTL_MEDIUM, "A330"),
        ("08:15", "TZX", DURATION_DOMESTIC, BUFFER_DOMESTIC, "A320"),
        ("08:15", "ESB", DURATION_DOMESTIC, BUFFER_DOMESTIC, "B739"),
        ("08:15", "LHR", DURATION_INTL_MEDIUM, BUFFER_INTL_MEDIUM, "A359"),
        ("08:15", "JFK", DURATION_INTL_LONG, BUFFER_INTL_LONG, "B772"),
        ("08:30", "AYT", DURATION_DOMESTIC, BUFFER_DOMESTIC, "A319"),
        ("08:30", "ADA", DURATION_DOMESTIC, BUFFER_DOMESTIC, "A320"),
        ("08:30", "FRA", DURATION_INTL_MEDIUM, BUFFER_INTL_MEDIUM, "A330"),
        ("08:30", "DXB", DURATION_INTL_MEDIUM, BUFFER_INTL_MEDIUM, "B788"),
        ("08:30", "AMS", DURATION_INTL_MEDIUM, BUFFER_INTL_MEDIUM, "A359"),
        ("08:30", "JFK", DURATION_INTL_LONG, BUFFER_INTL_LONG, "B788"),
    ]
    for i, (window, dest, duration, buffer_min, aircraft) in enumerate(surge_plan):
        dep_scheduled = _dep_scheduled_for_effective_window(window, buffer_min)
        surge_dep.append(record(
            airline_iata="TK", airline_icao="THY", flight_number=f"3{i:02d}",
            dep_iata="IST", arr_iata=dest,
            dep_scheduled=dep_scheduled, duration_minutes=duration,
            aircraft_icao=aircraft,
        ))
    # §10 Cancellation - surge bank'ındaki 3 uçuş T1'de cancelled.
    for flight_iata in ("TK300", "TK304", "TK309"):
        find(surge_dep, flight_iata)["status"] = "cancelled"

    t1_dep = t1_dep + surge_dep

    # §7 Passport surge - 10:00/10:15/10:30/10:45 efektif pencerelerine
    # 12 YENİ uluslararası arrival (widebody-ağırlıklı).
    surge_arr = []
    arr_surge_plan = [
        ("10:00", "CDG", "A330"), ("10:00", "LHR", "A359"), ("10:00", "DXB", "B772"),
        ("10:15", "FRA", "A330"), ("10:15", "AMS", "A359"), ("10:15", "JFK", "B788"),
        ("10:30", "CDG", "A330"), ("10:30", "DXB", "B772"), ("10:30", "LHR", "B788"),
        ("10:45", "AMS", "A330"), ("10:45", "FRA", "A359"), ("10:45", "JFK", "B772"),
    ]
    for i, (window, origin, aircraft) in enumerate(arr_surge_plan):
        arr_scheduled = _dep_scheduled_for_passport_window(window)
        duration = DURATION_INTL_LONG if origin == "JFK" else DURATION_INTL_MEDIUM
        dep_scheduled = arr_scheduled - timedelta(minutes=duration)
        surge_arr.append(record(
            airline_iata="TK" if i % 2 == 0 else "BA", airline_icao="THY" if i % 2 == 0 else "BAW",
            flight_number=f"4{i:02d}",
            dep_iata=origin, arr_iata="IST",
            dep_scheduled=dep_scheduled, duration_minutes=duration,
            aircraft_icao=aircraft,
        ))
    t1_arr = t1_arr + surge_arr

    # §8 Delay compression - TK101-104'ün dep_estimated_utc'sini 09:00
    # penceresine (effective) ortaklaştır (flight_key/scheduled SABİT).
    for idx in range(4):
        flight = find(t1_dep, f"TK10{idx + 1}")
        estimated = _dep_scheduled_for_effective_window("09:00", BUFFER_DOMESTIC) + timedelta(minutes=idx * 2)
        flight["dep_estimated"] = _fmt(estimated)
        flight["dep_estimated_utc"] = _fmt(estimated)
        flight["status"] = "active"

    # §9 Aircraft change - TK201/TK202: A320 -> B772.
    for flight_iata in ("TK201", "TK202"):
        find(t1_dep, flight_iata)["aircraft_icao"] = "B772"

    # ADIM 6C §D - arrival delay compression: TK601 (T0 effective
    # 09:30 penceresi) + TK602 (T0 effective 10:00 penceresi) T1'de
    # arr_estimated_utc'leri ORTAK bir pencereye (11:00 efektif,
    # mevcut 10:00-10:45 passport surge'ünden BİLİNÇLİ olarak ayrı)
    # taşınıyor - flight_key/scheduled SABİT.
    for flight_iata, minute_offset in (("TK601", 0), ("TK602", 2)):
        flight = find(t1_arr, flight_iata)
        estimated = _dt("10:45") + timedelta(minutes=1 + minute_offset)  # effective ~11:00-11:03
        flight["arr_estimated"] = _fmt(estimated)
        flight["arr_estimated_utc"] = _fmt(estimated)
        flight["status"] = "active"

    # ADIM 6C §D - arrival aircraft capacity change: TK701 A320 -> B788.
    find(t1_arr, "TK701")["aircraft_icao"] = "B788"

    # ADIM 6C §D - passport surge bank'ından bir uçuş cancelled (arrival
    # tarafında da cancellation-exclusion'ı doğrudan doğrulamak için).
    find(surge_arr, "TK406")["status"] = "cancelled"

    t1 = {"departure": t1_dep, "arrival": t1_arr}

    # ================= T2 (RECOVERY) =================
    t2_dep = copy.deepcopy(t1_dep)
    t2_arr = copy.deepcopy(t1_arr)

    # Surge departure bank'ı (cancelled OLMAYANLAR) GENİŞ bir aralığa
    # (08:00-10:00) yayılıp "actual" kazanıyor - yoğunluk dağılıyor.
    active_surge = [
        r for r in surge_dep if r["status"] != "cancelled"
    ]
    for i, r in enumerate(active_surge):
        target = find(t2_dep, r["flight_iata"])
        spread_minutes = int(i * (120 / max(len(active_surge) - 1, 1)))  # 08:00..10:00 aralığına yay
        actual = _dt("08:00") + timedelta(minutes=spread_minutes)
        target["dep_actual"] = _fmt(actual)
        target["dep_actual_utc"] = _fmt(actual)
        target["status"] = "active"

    # Passport surge bank'ı da (10:00-12:30 aralığına) yayılıyor - AMA
    # cancelled olan (TK406) HARİÇ: departure surge'ündeki AYNI kural
    # (bkz. `active_surge` yukarıda) - cancelled bir uçuşa "actual"
    # zaman verip status'unu "landed"e çevirmek, T1'de bilerek
    # cancelled işaretlenen uçuşu SESSİZCE geri açardı.
    active_surge_arr = [r for r in surge_arr if r["status"] != "cancelled"]
    for i, r in enumerate(active_surge_arr):
        target = find(t2_arr, r["flight_iata"])
        spread_minutes = int(i * (150 / max(len(active_surge_arr) - 1, 1)))
        actual = _dt("10:00") + timedelta(minutes=spread_minutes)
        target["arr_actual"] = _fmt(actual)
        target["arr_actual_utc"] = _fmt(actual)
        target["status"] = "landed"

    # TK101-104: gecikme "actual"laştı (finalize) - artık kalkmış.
    for idx in range(4):
        target = find(t2_dep, f"TK10{idx + 1}")
        target["dep_actual"] = target["dep_estimated"]
        target["dep_actual_utc"] = target["dep_estimated_utc"]
        target["status"] = "landed"

    # TK601/TK602: arrival gecikmesi de finalize oldu - artık inmiş.
    for flight_iata in ("TK601", "TK602"):
        target = find(t2_arr, flight_iata)
        target["arr_actual"] = target["arr_estimated"]
        target["arr_actual_utc"] = target["arr_estimated_utc"]
        target["status"] = "landed"
    # TK701: kapasite değişikliği (B788) KALICI - T2'de GERİ ALINMAZ.

    t2 = {"departure": t2_dep, "arrival": t2_arr}

    return t0, t1, t2, {
        "delay_targets": [f"TK10{i + 1}" for i in range(4)],
        "capacity_change_targets": ["TK201", "TK202"],
        "cancelled_targets": ["TK300", "TK304", "TK309"],
        "security_surge_flights": [r["flight_iata"] for r in surge_dep],
        "passport_surge_flights": [r["flight_iata"] for r in surge_arr],
        "arrival_delay_targets": ["TK601", "TK602"],
        "arrival_capacity_change_targets": ["TK701"],
        "arrival_cancelled_targets": ["TK406"],
    }


def build_control(
    airport: str, dep_count: int, arr_count: int, surge_count: int, surge_window: str,
    arrival_surge_count: int = 0, arrival_surge_window: str = "12:00",
):
    """
    SAW/ADB kontrol grupları. `surge_count`=0 VE `arrival_surge_count`=0
    ise (ADB) T0=T1=T2 BİREBİR aynıdır - gerçek bir kontrol grubu.

    `arrival_surge_count` - ADIM 6C §D: SAW için IST'ten KÜÇÜK, ayrı bir
    passport (international arrival) surge'ü. Departure surge ile AYNI
    desen (`_dep_scheduled_for_effective_window`/passport için
    `_dep_scheduled_for_passport_window`), sadece arrival tarafında.
    """
    t0_dep = baseline_schedule(airport, "departure", dep_count, start_number=100)
    t0_arr = baseline_schedule(airport, "arrival", arr_count, start_number=500)
    t0 = {"departure": t0_dep, "arrival": t0_arr}

    t1_dep = copy.deepcopy(t0_dep)
    bump = []
    if surge_count:
        for i in range(surge_count):
            dest, duration, buffer_min, pool = _route(i)
            aircraft = pool[i % len(pool)] if i % 4 else WIDE_MEDIUM[i % len(WIDE_MEDIUM)]
            dep_scheduled = _dep_scheduled_for_effective_window(surge_window, buffer_min)
            bump.append(record(
                airline_iata="TK", airline_icao="THY", flight_number=f"9{i:02d}",
                dep_iata=airport, arr_iata=dest,
                dep_scheduled=dep_scheduled, duration_minutes=duration,
                aircraft_icao=aircraft,
            ))
    t1_dep = t1_dep + bump

    t1_arr = copy.deepcopy(t0_arr)
    arr_bump = []
    if arrival_surge_count:
        arr_origins = INTL_MEDIUM_DESTS
        arr_aircraft = [WIDE_MEDIUM[0], "A321", "A320", WIDE_MEDIUM[1]]
        for i in range(arrival_surge_count):
            origin = arr_origins[i % len(arr_origins)]
            aircraft = arr_aircraft[i % len(arr_aircraft)]
            arr_scheduled = _dep_scheduled_for_passport_window(arrival_surge_window) + timedelta(minutes=i)
            dep_scheduled = arr_scheduled - timedelta(minutes=DURATION_INTL_MEDIUM)
            arr_bump.append(record(
                airline_iata="TK", airline_icao="THY", flight_number=f"8{i:02d}",
                dep_iata=origin, arr_iata=airport,
                dep_scheduled=dep_scheduled, duration_minutes=DURATION_INTL_MEDIUM,
                aircraft_icao=aircraft,
            ))
    t1_arr = t1_arr + arr_bump
    t1 = {"departure": t1_dep, "arrival": t1_arr}

    t2_dep = copy.deepcopy(t1_dep)
    for r in bump:
        target = find(t2_dep, r["flight_iata"])
        target["dep_actual"] = target["dep_time_utc"]
        target["dep_actual_utc"] = target["dep_time_utc"]
        target["status"] = "landed"
    t2_arr = copy.deepcopy(t1_arr)
    for i, r in enumerate(arr_bump):
        # Passport surge de (IST'in T2'sindeki gibi) GENİŞ bir aralığa
        # yayılıp "actual" kazanıyor - yeni bir surge EKLENMİYOR.
        target = find(t2_arr, r["flight_iata"])
        spread_minutes = int(i * (90 / max(len(arr_bump) - 1, 1)))
        actual = _dt(arrival_surge_window) + timedelta(minutes=spread_minutes)
        target["arr_actual"] = _fmt(actual)
        target["arr_actual_utc"] = _fmt(actual)
        target["status"] = "landed"
    t2 = {"departure": t2_dep, "arrival": t2_arr}

    return t0, t1, t2


def _paginate(records: list[dict], page_size: int = 40) -> list[tuple[list[dict], bool]]:
    if not records:
        return [([], False)]
    pages = []
    for start in range(0, len(records), page_size):
        chunk = records[start:start + page_size]
        has_more = start + page_size < len(records)
        pages.append((chunk, has_more))
    return pages


def _write_pages(round_name: str, airport: str, direction: str, records: list[dict]) -> None:
    pages = _paginate(records)
    for i, (chunk, has_more) in enumerate(pages, start=1):
        payload = {
            "_mock_disclaimer": (
                "SYNTHETIC OPERATIONAL TEST DATA - NOT A REAL AIRLABS RESPONSE. "
                "ADIM 6A operasyonel senaryo verisi; gerçek bir AirLabs API "
                "çağrısından gelmemiştir. JSON zarfı/alan şeması gerçek "
                "`schedules` kontratıyla (bkz. tests/fixtures/airlabs_mock/) "
                "aynıdır, sadece İÇERİK sentetiktir."
            ),
            "request": {
                "host": "airlabs.co",
                "method": "schedules",
                "params": {
                    ("dep_iata" if direction == "departure" else "arr_iata"): airport,
                },
                "version": 9,
                "has_more": has_more,
                "total_items": len(records),
            },
            "response": chunk,
        }
        filename = f"schedules_{airport}_{direction}_page_{i}.json"
        path = os.path.join(OUT_DIR, round_name, filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)


def main() -> None:
    ist_t0, ist_t1, ist_t2, ist_meta = build_ist()
    saw_t0, saw_t1, saw_t2 = build_control(
        "SAW", 35, 35, surge_count=5, surge_window="08:00",
        arrival_surge_count=4, arrival_surge_window="12:00",   # ADIM 6C §D - IST'ten küçük passport surge
    )
    adb_t0, adb_t1, adb_t2 = build_control("ADB", 25, 25, surge_count=0, surge_window="08:00")

    rounds = {
        "t0": {"IST": ist_t0, "SAW": saw_t0, "ADB": adb_t0},
        "t1": {"IST": ist_t1, "SAW": saw_t1, "ADB": adb_t1},
        "t2": {"IST": ist_t2, "SAW": saw_t2, "ADB": adb_t2},
    }

    for round_name, airports in rounds.items():
        for airport, directions in airports.items():
            for direction, records in directions.items():
                _write_pages(round_name, airport, direction, records)

    with open(os.path.join(OUT_DIR, "scenario_meta.json"), "w", encoding="utf-8") as handle:
        json.dump(ist_meta, handle, ensure_ascii=False, indent=1)

    for round_name, airports in rounds.items():
        for airport, directions in airports.items():
            total = len(directions["departure"]) + len(directions["arrival"])
            print(f"{round_name} {airport}: departure={len(directions['departure'])} "
                  f"arrival={len(directions['arrival'])} total={total}")


if __name__ == "__main__":
    main()
