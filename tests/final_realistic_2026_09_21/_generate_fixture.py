"""
ADIM (Realistic 9-Airport Dataset) - Bölüm 22-30/31 - bu betik
`tests/final_realistic_2026_09_21/` altına GERÇEK production JSON
şemasıyla (mevcut `Delays - Type Departures.json`/`Delays - Type
Arrivals.json`/`flights_live.json` şeması - YENİ şema İCAT EDİLMEDİ)
9 gerçek havalimanı için throughput-calibrated synthetic flight fixture
üretir.

SADECE bu klasöre yazar - gerçek `data/*.json`/`database.sqlite` HİÇ
açılmaz/yazılmaz.

REAL/REFERENCE (bkz. rapor Bölüm 24/25): airport identity, IATA/ICAO,
timezone, scale, annual throughput, avg/day - hepsi gerçek/resmi
kaynaktan (kaynaklar validation_report.md'de).
SYNTHETIC: flight number, schedule, banks, route eşleştirmeleri,
estimated/actual zamanlama - GERÇEK yayınlanmış tarife DEĞİLDİR.

Model realism limitation (Bölüm 26): load factor YOK - production
demand = resolved seat capacity (AircraftCapacityService.resolve()).
Bu yüzden generated flight count, target_daily_passengers'a (annual/365)
"makul yakın" GERÇEK production-resolved demand üretecek şekilde
(flight count + aircraft mix + 24h dağılım ile, load factor EKLENMEDEN)
hesaplanır - LARGE havalimanları için bu YÜZLERCE flight gerektirir,
fixture boyutunu küçültmek için sayı KEYFİ küçültülmedi (bkz. kullanıcı
talimatı).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_DIR = Path(__file__).resolve().parent
DATE = datetime(2026, 9, 21)

# ========================================================================
# Bölüm 28 - Aircraft mix (hepsi production AircraftCapacity tablosunda
# GERÇEKTEN çözülüyor - bkz. rapor doğrulaması).
# (icao, capacity, weight)
#
# ADIM (Hourly Realism Fix) - "large" mix DAHA narrowbody-ağırlıklı
# yapıldı (widebody payı %40 -> %25). Gerekçe: gerçek büyük hub'larda
# (JFK/MAD/SIN) TOPLAM hareketin ÇOĞUNLUĞU narrowbody/domestic'tir -
# widebody uluslararası uçuşlar daha AZ SAYIDA ama daha YÜKSEK
# kapasiteli. Eski mix (avg~267.85 kapasite/flight) SAYICA AZ ama
# HER BİRİ BÜYÜK flight'lar ürettiği için, aynı günlük target'a
# ulaşmak için gereken flight SAYISI azalıyor ve TEK SAATE düşen
# yolcu hacmi (aynı saat-ağırlığı oranında bile) gerçekçi-üstü
# yoğunlaşıyordu (bkz. rapor Bölüm 44 bulgusu). Daha küçük ortalama
# kapasiteli mix, AYNI target için DAHA FAZLA (daha küçük) flight
# gerektirir - bu da saatlik dağılımı doğal olarak inceltir.
# ========================================================================
MIX = {
    "large": [
        ("A320", 150, 0.25), ("A20N", 180, 0.25), ("A21N", 220, 0.10),
        ("B738", 162, 0.15), ("B772", 305, 0.15), ("B744", 416, 0.07),
        ("A388", 555, 0.03),
    ],
    "medium": [
        ("A320", 150, 0.35), ("B738", 162, 0.35), ("E170", 70, 0.10),
        ("E190", 114, 0.10), ("CRJ9", 90, 0.10),
    ],
    "small": [
        ("DHC6", 19, 0.35), ("SF34", 34, 0.25), ("AT72", 72, 0.25),
        ("DH8D", 78, 0.10), ("E170", 70, 0.05),
    ],
}

# ========================================================================
# Bölüm 30/Hourly Realism Fix - Her havalimanı için AYRI, ARAŞTIRILMIŞ
# departure/arrival saat ağırlıkları. Kaynaklar/gerekçe AIRPORTS
# sözlüğündeki "basis"/"sources" alanlarında (bkz. validation_report.md).
# Peak "bank"lar artık TEK bir baskın saat değil, GENİŞ bir PLATO
# (3-4 saat, birbirine yakın ağırlıklarla) - gerçek havalimanlarının
# "peak hour" istatistiği bile komşu saatlere yakın trafik taşıdığını
# gösteriyor (bkz. JFK/SIN TripWaffle verisi - peak/ortalama oranı
# ~1.8-2x, 8-10x DEĞİL).
# ========================================================================

def _closed_window_weights(open_hours: set[int], peak_hours: set[int] | None = None) -> dict[int, float]:
    """Sadece belirtilen saatlerde faaliyet olan (gece kapalı) küçük havalimanları için."""
    peak_hours = peak_hours or set()
    return {h: (2.5 if h in peak_hours else (1.0 if h in open_hours else 0.0)) for h in range(24)}


DEP_HOUR_PROFILES = {
    # JFK - REAL (TripWaffle aviation dataset): departure peak 19:00
    # local, quiet 04:00. Evening uluslararası "wave" (19-22h) GENİŞ
    # plato olarak modellendi (tek saat DEĞİL) - kaynak metninde "60+
    # widebody 19-22h arası" ifadesi zaten bir PLATO tanımlıyor.
    "JFK": {
        0: 0.3, 1: 0.3, 2: 0.3, 3: 0.3, 4: 0.2, 5: 0.6,
        6: 1.2, 7: 1.8, 8: 2.0, 9: 1.8, 10: 1.5, 11: 1.8, 12: 1.8,
        13: 1.6, 14: 1.5, 15: 1.6, 16: 1.8, 17: 2.0, 18: 2.6, 19: 2.8,
        20: 2.6, 21: 2.2, 22: 1.2, 23: 0.6,
    },
    # MAD - DERIVED (AENA-yakın kaynaklar: security 06-09 ve 16-19 iki
    # ayrı peak, 11-14 sakin). Çift-plato (morning+evening) modellendi.
    "MAD": {
        0: 0.2, 1: 0.2, 2: 0.2, 3: 0.2, 4: 0.3, 5: 0.8,
        6: 2.2, 7: 2.6, 8: 2.4, 9: 1.6, 10: 1.3, 11: 1.1, 12: 1.1,
        13: 1.2, 14: 1.3, 15: 1.6, 16: 2.2, 17: 2.5, 18: 2.4, 19: 2.0,
        20: 1.4, 21: 1.0, 22: 0.6, 23: 0.3,
    },
    # SIN - REAL (TripWaffle): departure peak 09:00, quiet 04:00 (ama
    # Changi 24/7 çalışıyor, hiç sıfır değil).
    "SIN": {
        0: 0.8, 1: 0.6, 2: 0.5, 3: 0.4, 4: 0.4, 5: 0.6,
        6: 1.0, 7: 1.6, 8: 2.2, 9: 2.6, 10: 2.2, 11: 1.8, 12: 1.6,
        13: 1.5, 14: 1.4, 15: 1.5, 16: 1.6, 17: 1.8, 18: 1.8, 19: 1.6,
        20: 1.4, 21: 1.2, 22: 1.0, 23: 0.9,
    },
    # BOH - DERIVED (Bournemouth Airport'un kendi duyurusu: "morning
    # departures peak + midday arrivals peak" - LCC/charter üssü
    # turnaround deseni). Departure ağırlığı sabah erken.
    "BOH": {h: (2.5 if h in (6, 7, 8) else (1.0 if 5 <= h <= 20 else 0.1)) for h in range(24)},
    # CID - DERIVED (hub-besleme dalga deseni: AA->DFW, DL->MSP/ATL,
    # UA->ORD/DEN gibi büyük hub bağlantılarını beslemek için sabah/
    # öğlen/akşam 3 dalga - gerçek 01:50-23:59 operasyon aralığı
    # doğrulandı [flightsfrom.com], ama saatlik granülerlik yok).
    "CID": {h: (2.0 if h in (6, 7, 12, 13, 17, 18) else (1.0 if 5 <= h <= 21 else 0.15)) for h in range(24)},
    # ASP - REAL (flightsfrom.com ADL<->ASP tarifesi: 07:45-14:35
    # arası yoğunlaşma, sabah erken/akşam geç uçuş YOK).
    "ASP": _closed_window_weights(set(range(7, 15)), peak_hours={9, 10, 11}),
    # MVY - DERIVED (Cape Air yüksek-frekans commuter modeli: gün
    # boyu sürekli servis, güçlü tek peak YOK, gece kapalı).
    "MVY": _closed_window_weights(set(range(6, 22)), peak_hours={7, 8, 17, 18}),
    # ISC - REAL (Skybus resmi tarife: LEQ->ISC 08:15-17:20, ~7 uçuş/gün).
    "ISC": _closed_window_weights(set(range(8, 18)), peak_hours={8, 9}),
    # LEQ - REAL (Skybus resmi tarife: aynı kaynak, ISC->LEQ 08:50-17:55).
    "LEQ": _closed_window_weights(set(range(8, 18)), peak_hours={9, 10}),
}

# Arrival profilleri - JFK/SIN için AYRI araştırılmış gerçek arrival
# peak'i var (departure'dan FARKLI saat); diğerlerinde dep profiliyle
# AYNI genel şekil kullanılıyor (ayrı arrival verisi bulunamadı -
# SYNTHETIC ASSUMPTION, açıkça işaretli).
ARR_HOUR_PROFILES = dict(DEP_HOUR_PROFILES)
ARR_HOUR_PROFILES["JFK"] = {
    # REAL (TripWaffle): arrival peak 12:00 local.
    0: 0.3, 1: 0.3, 2: 0.3, 3: 0.3, 4: 0.2, 5: 0.6,
    6: 1.2, 7: 1.6, 8: 1.8, 9: 2.0, 10: 2.2, 11: 2.6, 12: 2.8,
    13: 2.6, 14: 2.2, 15: 1.8, 16: 1.6, 17: 1.6, 18: 1.6, 19: 1.4,
    20: 1.2, 21: 1.0, 22: 0.8, 23: 0.5,
}
ARR_HOUR_PROFILES["SIN"] = {
    # REAL (TripWaffle): arrival peak 17:00 local.
    0: 0.8, 1: 0.6, 2: 0.5, 3: 0.4, 4: 0.4, 5: 0.6,
    6: 1.0, 7: 1.2, 8: 1.4, 9: 1.5, 10: 1.6, 11: 1.6, 12: 1.6,
    13: 1.6, 14: 1.8, 15: 2.0, 16: 2.4, 17: 2.6, 18: 2.4, 19: 2.0,
    20: 1.6, 21: 1.3, 22: 1.1, 23: 0.9,
}

# ADIM (Realistic Fixture - Previous-Day Leakage Fix, KORUNDU) -
# production `effective_time()` (DEĞİŞTİRİLMEDİ) departure için sabit
# -120dk uyguluyor; yerel 00:00/01:00 departure bu offset sonrası BİR
# ÖNCEKİ yerel güne düşer (bkz. önceki tur raporu). Her havalimanın
# KENDİ departure profilinden hour=0/1 çıkarılıyor - arrival etkilenmiyor.
DEPARTURE_HOUR_WEIGHTS_BY_AIRPORT = {
    code: {h: (0.0 if h in (0, 1) else w) for h, w in profile.items()}
    for code, profile in DEP_HOUR_PROFILES.items()
}

AIRPORTS = {
    "JFK": dict(
        scale="large", tz="America/New_York", target=173_425,
        domestic=["LAX", "ORD", "ATL", "DFW", "MIA", "BOS"],
        intl=["LHR", "CDG", "FRA", "NRT", "GRU", "DXB"],
        domestic_ratio=0.40,
        airlines=["AA", "DL", "UA", "B6"],
        intl_airlines=["BA", "AF", "LH", "JL", "LA", "EK"],
        hourly_basis="REAL",
        hourly_sources=["TripWaffle JFK busyness/peak-hour dataset (2026)"],
    ),
    "MAD": dict(
        scale="large", tz="Europe/Madrid", target=181_362,
        domestic=["BCN", "VLC", "AGP", "PMI", "SVQ", "BIO"],
        intl=["LHR", "CDG", "FCO", "GRU", "JFK", "ICN"],
        domestic_ratio=0.30,
        airlines=["IB", "UX", "VY", "I2"],
        intl_airlines=["BA", "AF", "AZ", "LA", "DL", "KE"],
        hourly_basis="DERIVED",
        hourly_sources=["Parkos/FlightQueue security-peak commentary (06-09 & 16-19 dual peak, 11-14 quiet) - secondary aviation-travel sources, not AENA raw hourly counts"],
    ),
    "SIN": dict(
        scale="large", tz="Asia/Singapore", target=185_479,
        domestic=[],
        intl=["LHR", "SYD", "NRT", "DXB", "JFK", "ICN"],
        domestic_ratio=0.0,
        airlines=[],
        intl_airlines=["SQ", "BA", "QF", "JL", "EK", "KE"],
        hourly_basis="REAL",
        hourly_sources=["TripWaffle SIN busyness/peak-hour dataset (2026)"],
    ),
    "BOH": dict(
        scale="medium", tz="Europe/London", target=2_740,
        domestic=["MAN", "EDI", "NCL"],
        intl=["AGP", "ALC", "PMI"],
        domestic_ratio=0.35,
        airlines=["BA", "LM", "EZ"],
        intl_airlines=["FR", "U2"],
        hourly_basis="DERIVED",
        hourly_sources=["Bournemouth Airport public statement (choosewhere.com secondary): 'morning departures peak and midday arrivals peak' - LCC/charter turnaround pattern"],
    ),
    "CID": dict(
        scale="medium", tz="America/Chicago", target=4_110,
        domestic=["ORD", "DEN", "DFW", "MSP"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["AA", "UA", "DL", "WN"],
        intl_airlines=[],
        hourly_basis="DERIVED",
        hourly_sources=["kupi.com/flightsfrom.com CID timetable confirms 01:50-23:59 operating span; hub-feeder wave pattern (AA/DL/UA/WN connections) derived from standard regional-airport-to-hub scheduling practice, not exact published hourly counts"],
    ),
    "ASP": dict(
        scale="medium", tz="Australia/Darwin", target=978,
        domestic=["SYD", "MEL", "ADL", "DRW"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["QF", "VA", "JQ"],
        intl_airlines=[],
        hourly_basis="REAL",
        hourly_sources=["FlightsFrom.com ADL-ASP published schedule: departures 07:45-14:05, returns 09:35-14:35 - no early-morning/late-night scheduled service"],
    ),
    "MVY": dict(
        scale="small", tz="America/New_York", target=441,
        domestic=["BOS", "JFK", "EWR"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["9K", "B6"],
        intl_airlines=[],
        hourly_basis="DERIVED",
        hourly_sources=["Cape Air (capeair.com) high-frequency commuter operating model - continuous daytime service, no single dominant peak documented"],
    ),
    "ISC": dict(
        scale="small", tz="Europe/London", target=187,
        domestic=["EXT", "NQY", "LHR"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["5W"],
        intl_airlines=[],
        hourly_basis="REAL",
        hourly_sources=["Skybus official timetable (islesofscilly-travel.co.uk): LEQ->ISC departures 08:15-17:20, ~7 flights/day, no evening/night service"],
    ),
    "LEQ": dict(
        scale="small", tz="Europe/London", target=136,
        domestic=["EXT", "NQY", "BRS"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["5W"],
        intl_airlines=[],
        hourly_basis="REAL",
        hourly_sources=["Skybus official timetable (islesofscilly-travel.co.uk): ISC->LEQ departures 08:50-17:55, no evening/night service"],
    ),
}


def _weighted_average_capacity(scale: str) -> float:
    return sum(cap * w for _, cap, w in MIX[scale])


def _allocate_by_weight(n: int, keyed_weights: list[tuple]) -> dict:
    """
    Largest-remainder yöntemiyle n birimi weight'lere göre tam sayı
    dağıtır. `keyed_weights`: [(key, weight), ...] - key HERHANGİ bir
    hashable değer olabilir (saat int'i, aircraft ICAO string'i).
    """
    total_weight = sum(w for _, w in keyed_weights)
    raw = [(key, n * w / total_weight) for key, w in keyed_weights]
    floors = [(key, int(v)) for key, v in raw]
    remainder = n - sum(c for _, c in floors)
    fracs = sorted(range(len(raw)), key=lambda i: raw[i][1] - floors[i][1], reverse=True)
    result = dict(floors)
    for i in fracs[:remainder]:
        key = floors[i][0]
        result[key] = result.get(key, 0) + 1
    return result


def _allocate_hours(n: int, weights: dict[int, float]) -> dict[int, int]:
    return _allocate_by_weight(n, list(weights.items()))


def plan_flight_count(airport: str) -> int:
    """
    Bölüm 27 - flight count SADECE `target_daily_passengers`'a en yakın
    tam sayıyı üretecek şekilde hesaplanır (round(target/avg_capacity)).
    KEYFİ bir minimum YOK.
    """
    cfg = AIRPORTS[airport]
    avg_cap = _weighted_average_capacity(cfg["scale"])
    n = round(cfg["target"] / avg_cap)
    return max(n, 4)


def _greedy_small_target_aircraft(target: int, scale: str, max_n: int = 14) -> list[str]:
    """
    Bölüm 27 - ÇOK küçük target'lar (SMALL tier, birkaç yüz yolcu/gün)
    için ağırlıklı-yuvarlama (`_allocate_by_weight`) tam sayı
    quantization'ı yüzünden büyük % sapma üretebiliyor (tek bir
    flight'ın eklenmesi/eklenmemesi toplamı %20-40 oynatabiliyor). Bu
    yüzden küçük target'larda AÇGÖZLÜ (greedy) bir seçim kullanılır:
    her adımda, toplamı target'a EN YAKIN yapan aircraft tipi eklenir -
    weight'ler yine de aircraft TERCİH sırasını belirler (en yüksek
    weight'li tip eşit derecede iyi seçeneklerde ÖNCELİKLİ), ama
    quantization artığı KEYFİ bir tam sayı yuvarlamasına bırakılmaz.
    """
    options = sorted(MIX[scale], key=lambda m: -m[2])  # weight'e göre öncelik
    chosen: list[str] = []
    total = 0
    while len(chosen) < max_n:
        best = None
        best_diff = None
        for icao, cap, _ in options:
            candidate_diff = abs((total + cap) - target)
            if best_diff is None or candidate_diff < best_diff:
                best_diff = candidate_diff
                best = (icao, cap)
        # Eklemek toplamı target'tan UZAKLAŞTIRIYORSA dur.
        if best_diff is not None and best_diff >= abs(total - target) and chosen:
            break
        chosen.append(best[0])
        total += best[1]
    return chosen


def _aircraft_cycle(n: int, scale: str) -> list[str]:
    """
    Her aircraft tipinden weight'e orantılı sayı üretir, sonra tipleri
    ARDIŞIK yığılmayacak şekilde (round-robin) INTERLEAVE eder - Bölüm
    9/10'daki "aynı dakika/aynı tip fazla düzenli yığılma" endişesiyle
    AYNI ilke (bkz. önceki turdaki CDG 24h fixture raporu).
    """
    counts = _allocate_by_weight(n, [(icao, w) for icao, _, w in MIX[scale]])
    buckets: dict[str, list[str]] = {icao: [icao] * c for icao, c in counts.items() if c > 0}
    keys = list(buckets.keys())
    interleaved = []
    i = 0
    while any(buckets[k] for k in keys):
        k = keys[i % len(keys)]
        if buckets[k]:
            interleaved.append(buckets[k].pop())
        i += 1
    return interleaved


def _capacity_of(icao: str, scale: str) -> int:
    return next(cap for code, cap, _ in MIX[scale] if code == icao)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def build_records_for_airport(airport: str) -> tuple[list[dict], list[dict], dict]:
    cfg = AIRPORTS[airport]
    scale = cfg["scale"]
    tz = ZoneInfo(cfg["tz"])

    if plan_flight_count(airport) <= 40:
        # Bölüm 27 - küçük target'larda (SMALL tier VE küçük MEDIUM
        # target'lar, ör. ASP) greedy seçim (bkz. fonksiyon docstring'i)
        # - departure/arrival YARI YARIYA bağımsız hedeflenir (her biri
        # target/2'ye greedy yaklaşır), toplamda quantization sapması
        # ağırlıklı-yuvarlamadan çok daha küçük kalır.
        half_target = cfg["target"] / 2
        dep_aircraft = _greedy_small_target_aircraft(round(half_target), scale)
        arr_aircraft = _greedy_small_target_aircraft(round(half_target), scale)
        n_dep, n_arr = len(dep_aircraft), len(arr_aircraft)
        n_total = n_dep + n_arr
    else:
        n_total = plan_flight_count(airport)
        n_dep = n_total // 2
        n_arr = n_total - n_dep
        dep_aircraft = _aircraft_cycle(n_dep, scale)
        arr_aircraft = _aircraft_cycle(n_arr, scale)

    dep_hours = _allocate_hours(n_dep, DEPARTURE_HOUR_WEIGHTS_BY_AIRPORT[airport])
    arr_hours = _allocate_hours(n_arr, ARR_HOUR_PROFILES[airport])

    n_dep_intl = round(n_dep * (1 - cfg["domestic_ratio"])) if cfg["intl"] else 0
    n_arr_intl = round(n_arr * (1 - cfg["domestic_ratio"])) if cfg["intl"] else 0

    departures = []
    arrivals = []
    flight_num = 700
    idx = 0
    dep_demand = 0
    arr_demand = 0

    for hour, count in sorted(dep_hours.items()):
        for i in range(count):
            minute = int(i * 60 / count) if count else 0
            local_dt = datetime(DATE.year, DATE.month, DATE.day, hour, minute, tzinfo=tz)
            dep_utc = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

            is_intl = idx < n_dep_intl and cfg["intl"]
            if is_intl:
                partner = cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=8)
            else:
                partner = cfg["domestic"][idx % len(cfg["domestic"])] if cfg["domestic"] else cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["airlines"][idx % len(cfg["airlines"])] if cfg["airlines"] else cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=1, minutes=45)

            aircraft = dep_aircraft[idx]
            cap = _capacity_of(aircraft, scale)
            dep_demand += cap
            arr_utc = dep_utc + duration
            flight_num += 1
            departures.append({
                "airline_iata": airline, "airline_icao": None,
                "flight_iata": f"{airline}{flight_num}", "flight_icao": None,
                "flight_number": str(flight_num),
                "dep_iata": airport, "dep_icao": None,
                "dep_terminal": "1", "dep_gate": None,
                "dep_time": _fmt(local_dt.replace(tzinfo=None)),
                "dep_time_utc": _fmt(dep_utc),
                "dep_estimated": _fmt(local_dt.replace(tzinfo=None)),
                "dep_estimated_utc": _fmt(dep_utc),
                "dep_actual": _fmt(local_dt.replace(tzinfo=None)),
                "dep_actual_utc": _fmt(dep_utc),
                "arr_iata": partner, "arr_icao": None,
                "arr_terminal": None, "arr_gate": None, "arr_baggage": None,
                "arr_time": _fmt(arr_utc), "arr_time_utc": _fmt(arr_utc),
                "arr_estimated": _fmt(arr_utc), "arr_estimated_utc": _fmt(arr_utc),
                "cs_airline_iata": None, "cs_flight_number": None, "cs_flight_iata": None,
                "status": "scheduled",
                "duration": int(duration.total_seconds() // 60),
                "delayed": 0, "dep_delayed": 0, "arr_delayed": 0,
                "aircraft_icao": aircraft,
                "arr_time_ts": int(arr_utc.timestamp()), "dep_time_ts": int(dep_utc.timestamp()),
            })
            idx += 1

    idx = 0
    for hour, count in sorted(arr_hours.items()):
        for i in range(count):
            minute = int(i * 60 / count) if count else 0
            local_dt = datetime(DATE.year, DATE.month, DATE.day, hour, minute, tzinfo=tz)
            arr_utc = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

            is_intl = idx < n_arr_intl and cfg["intl"]
            if is_intl:
                partner = cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=8)
            else:
                partner = cfg["domestic"][idx % len(cfg["domestic"])] if cfg["domestic"] else cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["airlines"][idx % len(cfg["airlines"])] if cfg["airlines"] else cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=1, minutes=45)

            aircraft = arr_aircraft[idx]
            cap = _capacity_of(aircraft, scale)
            arr_demand += cap
            dep_utc = arr_utc - duration
            flight_num += 1
            arrivals.append({
                "airline_iata": airline, "airline_icao": None,
                "flight_iata": f"{airline}{flight_num}", "flight_icao": None,
                "flight_number": str(flight_num),
                "dep_iata": partner, "dep_icao": None,
                "dep_terminal": None, "dep_gate": None,
                "dep_time": _fmt(dep_utc), "dep_time_utc": _fmt(dep_utc),
                "dep_estimated": _fmt(dep_utc), "dep_estimated_utc": _fmt(dep_utc),
                "arr_iata": airport, "arr_icao": None,
                "arr_terminal": "1", "arr_gate": None, "arr_baggage": "1",
                "arr_time": _fmt(local_dt.replace(tzinfo=None)),
                "arr_time_utc": _fmt(arr_utc),
                "arr_estimated": _fmt(local_dt.replace(tzinfo=None)),
                "arr_estimated_utc": _fmt(arr_utc),
                "arr_actual": _fmt(local_dt.replace(tzinfo=None)),
                "arr_actual_utc": _fmt(arr_utc),
                "cs_airline_iata": None, "cs_flight_number": None, "cs_flight_iata": None,
                "status": "scheduled",
                "duration": int(duration.total_seconds() // 60),
                "delayed": 0, "dep_delayed": 0, "arr_delayed": 0,
                "aircraft_icao": aircraft,
                "arr_time_ts": int(arr_utc.timestamp()), "dep_time_ts": int(dep_utc.timestamp()),
            })
            idx += 1

    summary = {
        "airport": airport, "scale": scale, "target_daily": cfg["target"],
        "flight_count": n_total, "departures": n_dep, "arrivals": n_arr,
        "generated_demand": dep_demand + arr_demand,
        "dep_demand": dep_demand, "arr_demand": arr_demand,
    }
    return departures, arrivals, summary


def main() -> None:
    all_departures = []
    all_arrivals = []
    summaries = []
    for airport in AIRPORTS:
        deps, arrs, summary = build_records_for_airport(airport)
        all_departures.extend(deps)
        all_arrivals.extend(arrs)
        summaries.append(summary)
        diff_pct = (summary["generated_demand"] - summary["target_daily"]) / summary["target_daily"] * 100
        print(
            f"{airport}: flights={summary['flight_count']} "
            f"target={summary['target_daily']} generated={summary['generated_demand']} "
            f"diff={diff_pct:+.1f}%"
        )

    dep_payload = {"request": {"method": "delays", "params": {"type": "departures"}}, "response": all_departures, "terms": "synthetic-realistic-fixture"}
    arr_payload = {"request": {"method": "delays", "params": {"type": "arrivals"}}, "response": all_arrivals, "terms": "synthetic-realistic-fixture"}
    live_payload = {"request": {"method": "flights"}, "response": [], "terms": "synthetic-realistic-fixture"}

    with open(OUT_DIR / "Delays - Type Departures.json", "w", encoding="utf-8") as fh:
        json.dump(dep_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(OUT_DIR / "Delays - Type Arrivals.json", "w", encoding="utf-8") as fh:
        json.dump(arr_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(OUT_DIR / "flights_live.json", "w", encoding="utf-8") as fh:
        json.dump(live_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    with open(OUT_DIR / "_generation_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summaries, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print()
    print(f"Total departures: {len(all_departures)}, arrivals: {len(all_arrivals)}, total: {len(all_departures) + len(all_arrivals)}")


if __name__ == "__main__":
    main()
