"""
ADIM (Full-Scale 9-Airport Realistic Fixture, 2026-09-22) - bu betik
`tests/realistic_9_airports_2026_09_22/` altına GERÇEK production JSON
şemasıyla (mevcut `data/Delays - Type Departures.json`/`Delays - Type
Arrivals.json`/`data/response-delays.json` şeması - YENİ şema İCAT
EDİLMEDİ, alan adları BİREBİR aynı) 9 gerçek havalimanı için
movement-hedefli (passenger-hedefli DEĞİL) synthetic flight fixture
üretir.

SADECE bu klasöre yazar - gerçek `data/*.json`/`database.sqlite` HİÇ
açılmaz/yazılmaz. Production parser (`app/queue/ingestion/sources.py`)
DEĞİŞTİRİLMEDİ - bu betik SADECE onun beklediği şemada satır üretir.

Airport set (kullanıcı talimatıyla verilmiş, ARAŞTIRMA BU TURDA
TEKRARLANMADI):
  LARGE:  IST (~1499 hareket/gün), SAW (~755), AMS (~1308)
  MEDIUM: JMK (~130), TZX (~73), KOI (~28)
  SMALL:  ISC (~25), SOG (~16), JTY (~4)

Bölüm 1 farkı (önceki JFK/MAD/SIN fixture'ından): `target` burada
PASSENGER değil doğrudan GÜNLÜK HAREKET (movement=departure+arrival)
sayısıdır - flight count = target (round), passenger sayısı sadece
RAPORLANAN bir ÇIKTI (aircraft mix x capacity), bir HEDEF DEĞİL.

REAL/REFERENCE: airport identity (IATA/ICAO/timezone/scale) - hepsi
gerçek `data/flight_airports.sql` + `data/*_olcekli_havaalanlari.txt`
kaynağından (bu betik hiçbirini okumaz/değiştirmez, sadece
`replay()`'in zaten okuduğu gerçek dosyalarla tutarlı IATA kodları
kullanır). SYNTHETIC: flight number, exact minute-level schedule, route
partner eşleştirmeleri, saatlik ağırlık profilleri (aşağıda
"basis"'i "REAL" olanlar HARİÇ - ISC profili önceki turdan, GERÇEK
Skybus tarifesinden, DEĞİŞMEDEN yeniden kullanıldı).

Duplicate-demand önlemi: hiçbir route partner kodu bu 9 havalimanından
biri DEĞİLDİR (kasıtlı) - böylece aynı "uçuş" iki farklı fixture
havalimanında iki kez sayılma riski YOKTUR.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_DIR = Path(__file__).resolve().parent
DATE = datetime(2026, 9, 22)
UTC = ZoneInfo("UTC")

# ========================================================================
# Aircraft mix per ölçek - önceki (final_realistic_2026_09_21) turdan
# DEĞİŞMEDEN yeniden kullanıldı (narrowbody-ağırlıklı large mix - Bölüm
# 44 bulgusunun düzeltmesi zaten burada, tekrar araştırılmadı).
# (icao, capacity, weight)
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


def _closed_window_weights(open_hours: set[int], peak_hours: set[int] | None = None) -> dict[int, float]:
    """Sadece belirtilen saatlerde faaliyet olan (gece kapalı) küçük/orta havalimanları için."""
    peak_hours = peak_hours or set()
    return {h: (2.5 if h in peak_hours else (1.0 if h in open_hours else 0.0)) for h in range(24)}


# ========================================================================
# Saatlik ağırlık profilleri - "basis" alanı AIRPORTS sözlüğünde
# belirtilir. ISC profili tek istisna: önceki turdan (final_realistic_
# 2026_09_21), GERÇEK Skybus tarifesinden (islesofscilly-travel.co.uk),
# DEĞİŞMEDEN yeniden kullanıldı - "araştırmayı baştan yapma" talimatı
# BU şekilde uygulanmış oldu (aynı havalimanı, aynı gerçek kaynak).
# Diğer 8 havalimanı için: havalimanının BİLİNEN operasyonel karakteri
# (hub/LCC/mevsimsel-turistik/ada-besleme/gece kapalı bölgesel) baz
# alınarak DERIVED, makul plato-şekilli profiller (tek keskin pik YOK -
# önceki turun "Hourly Realism Fix" ilkesiyle TUTARLI).
# ========================================================================
DEP_HOUR_PROFILES = {
    # IST - DERIVED: 7/24 mega-hub (Turkish Airlines ana hub'ı), sürekli
    # "wave" yapısı - gece de belirgin trafik var (uzun-menzil gece
    # kalkışları), sabah/akşam banklarında plato.
    "IST": {
        0: 1.2, 1: 1.0, 2: 0.8, 3: 0.8, 4: 1.0, 5: 1.4,
        6: 1.8, 7: 2.2, 8: 2.4, 9: 2.0, 10: 1.8, 11: 1.8, 12: 1.8,
        13: 1.8, 14: 1.8, 15: 1.9, 16: 2.0, 17: 2.2, 18: 2.4, 19: 2.4,
        20: 2.2, 21: 2.0, 22: 1.8, 23: 1.4,
    },
    # SAW - DERIVED: LCC (Pegasus) leisure hub'ı, sabah/öğlen/akşam
    # yoğun banklar, gece nispeten sakin ama sıfır değil (uçak
    # utilizasyonu için erken/geç uçuşlar tipik LCC pratiği).
    "SAW": {
        0: 0.3, 1: 0.2, 2: 0.2, 3: 0.3, 4: 0.6, 5: 1.4,
        6: 2.2, 7: 2.4, 8: 2.0, 9: 1.6, 10: 1.4, 11: 1.4, 12: 1.6,
        13: 1.6, 14: 1.5, 15: 1.6, 16: 1.8, 17: 2.2, 18: 2.4, 19: 2.0,
        20: 1.6, 21: 1.2, 22: 0.8, 23: 0.4,
    },
    # AMS - DERIVED: Avrupa hub'larının tipik ÇOK-BANKLI (multi-wave)
    # yapısı (KLM/SkyTeam bağlantı dalgaları) - gece gürültü kısıtı
    # nedeniyle 00-05 belirgin düşük.
    "AMS": {
        0: 0.3, 1: 0.2, 2: 0.1, 3: 0.1, 4: 0.3, 5: 1.0,
        6: 2.0, 7: 2.4, 8: 2.2, 9: 1.8, 10: 1.6, 11: 1.6, 12: 1.8,
        13: 1.8, 14: 1.7, 15: 1.8, 16: 2.0, 17: 2.2, 18: 2.0, 19: 1.8,
        20: 1.6, 21: 1.4, 22: 1.0, 23: 0.5,
    },
    # JMK - DERIVED: mevsimsel turistik ada, gece KAPALI (küçük
    # regional pist, gece ışıklandırma/operasyon kısıtı), sabah ATH
    # shuttle + öğlen/akşam charter platosu.
    "JMK": _closed_window_weights(set(range(6, 23)), peak_hours={7, 8, 9, 17, 18}),
    # TZX - DERIVED: Türkiye iç hat besleme (IST/SAW/ESB hub bağlantısı)
    # + sınırlı uluslararası charter/hac - CID şablonuyla AYNI ilke
    # (sabah/öğlen/akşam 3 dalga).
    "TZX": {h: (2.0 if h in (6, 7, 12, 13, 17, 18) else (1.0 if 5 <= h <= 22 else 0.15)) for h in range(24)},
    # KOI - DERIVED: Orkney ada-içi/anakara besleme (Loganair), gündüz
    # operasyonu (gece KAPALI - küçük bölgesel pist), hafif sabah/öğlen
    # sonrası plato.
    "KOI": _closed_window_weights(set(range(6, 19)), peak_hours={7, 8, 15, 16}),
    # ISC - REAL (Skybus resmi tarife, ÖNCEKİ turdan DEĞİŞMEDEN
    # yeniden kullanıldı): LEQ->ISC 08:15-17:20, ~7 uçuş/gün.
    "ISC": _closed_window_weights(set(range(8, 18)), peak_hours={8, 9}),
    # SOG - DERIVED: Norveç bölgesel (Widerøe), gündüz operasyonu,
    # sabah/öğleden sonra iki hafif plato.
    "SOG": _closed_window_weights(set(range(6, 20)), peak_hours={7, 8, 15, 16}),
    # JTY - DERIVED: çok küçük Yunan adası, günde sadece birkaç uçuş,
    # öğlen civarı tek dar pencere.
    "JTY": _closed_window_weights(set(range(9, 19)), peak_hours={11, 12}),
}

# Arrival profilleri - ayrı araştırılmış gerçek arrival verisi
# bulunamadı (SYNTHETIC ASSUMPTION, açıkça işaretli) - departure
# profiliyle AYNI genel şekil kullanılıyor (önceki turdaki JFK/SIN gibi
# İSTİSNAİ ayrı arrival verisi burada YOK).
ARR_HOUR_PROFILES = dict(DEP_HOUR_PROFILES)

# ADIM (Previous-Day Leakage Fix, önceki turdan KORUNDU) - production
# `effective_time()` (DEĞİŞTİRİLMEDİ) departure için sabit -120dk
# uyguluyor; yerel 00:00/01:00 departure bu offset sonrası BİR ÖNCEKİ
# yerel güne düşebilir - her havalimanın KENDİ departure profilinden
# hour=0/1 çıkarılır (arrival etkilenmiyor, +15dk offset sadece ileri
# kaydırır).
DEPARTURE_HOUR_WEIGHTS_BY_AIRPORT = {
    code: {h: (0.0 if h in (0, 1) else w) for h, w in profile.items()}
    for code, profile in DEP_HOUR_PROFILES.items()
}

# ========================================================================
# Havalimanı tanımları. `target` = GÜNLÜK TOPLAM HAREKET (departure+
# arrival) - kullanıcının verdiği araştırılmış hedef. Route partner'lar
# KASITLI olarak bu 9 havalimanının HİÇBİRİ DEĞİL (duplicate-demand
# önlemi - bkz. modül docstring'i).
# ========================================================================
AIRPORTS = {
    "IST": dict(
        scale="large", tz="Europe/Istanbul", target=1499,
        domestic=["ESB", "ADB", "AYT", "GZT", "DIY", "TZX", "VAN"],
        intl=["LHR", "FRA", "DXB", "JFK", "CDG", "AMS", "DOH"],
        domestic_ratio=0.35,
        airlines=["TK", "PC"],
        intl_airlines=["TK", "EK", "LH", "BA", "QR", "KL"],
    ),
    "SAW": dict(
        scale="large", tz="Europe/Istanbul", target=755,
        domestic=["ESB", "ADB", "AYT", "GZT", "TZX", "VAN"],
        intl=["BER", "DUS", "VIE", "BCN", "BRU"],
        domestic_ratio=0.55,
        airlines=["PC", "XQ"],
        intl_airlines=["PC", "XQ", "W6"],
    ),
    "AMS": dict(
        scale="large", tz="Europe/Amsterdam", target=1308,
        domestic=[],
        intl=["LHR", "JFK", "CDG", "FRA", "MAD", "DXB", "SIN"],
        domestic_ratio=0.0,
        airlines=[],
        intl_airlines=["KL", "DL", "AF", "BA", "LH", "EK"],
    ),
    "JMK": dict(
        scale="medium", tz="Europe/Athens", target=130,
        domestic=["ATH", "SKG"],
        intl=["LHR", "FCO", "MUC", "CDG"],
        domestic_ratio=0.60,
        airlines=["A3", "OA", "GQ"],
        intl_airlines=["FR", "U2", "EZY"],
    ),
    "TZX": dict(
        scale="medium", tz="Europe/Istanbul", target=73,
        domestic=["IST", "SAW", "ESB", "ADB"],
        intl=["DXB", "DUS"],
        domestic_ratio=0.85,
        airlines=["TK", "PC", "XQ"],
        intl_airlines=["FZ", "PC"],
    ),
    "KOI": dict(
        scale="medium", tz="Europe/London", target=28,
        domestic=["EDI", "GLA", "ABZ", "SYY", "LWK", "WRY"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["LM"],
        intl_airlines=[],
    ),
    "ISC": dict(
        scale="small", tz="Europe/London", target=25,
        domestic=["LEQ", "EXT", "NQY"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["5W"],
        intl_airlines=[],
    ),
    "SOG": dict(
        scale="small", tz="Europe/Oslo", target=16,
        domestic=["OSL", "BGO"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["WF"],
        intl_airlines=[],
    ),
    "JTY": dict(
        scale="small", tz="Europe/Athens", target=4,
        domestic=["ATH"],
        intl=[],
        domestic_ratio=1.0,
        airlines=["OA", "GQ"],
        intl_airlines=[],
    ),
}


def _allocate_by_weight(n: int, keyed_weights: list[tuple]) -> dict:
    """Largest-remainder yöntemiyle n birimi weight'lere göre tam sayı dağıtır."""
    total_weight = sum(w for _, w in keyed_weights)
    if total_weight <= 0 or n <= 0:
        return {key: 0 for key, _ in keyed_weights}
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


def _greedy_small_target_aircraft(target_count: int, scale: str, max_n: int = 30) -> list[str]:
    """
    Çok küçük flight-count hedefleri (SMALL tier, tek hane/onlar) için
    ağırlıklı-yuvarlamanın büyük % sapma üretmesini önler - hedefe
    doğrudan flight SAYISI olarak greedy yaklaşır (her adımda en yüksek
    weight'li tipi seçer, tam target_count adet üretir).
    """
    options = sorted(MIX[scale], key=lambda m: -m[2])
    chosen: list[str] = []
    i = 0
    while len(chosen) < target_count and len(chosen) < max_n:
        icao = options[i % len(options)][0]
        chosen.append(icao)
        i += 1
    return chosen


def _aircraft_cycle(n: int, scale: str) -> list[str]:
    """Ağırlığa orantılı sayı üretir, ARDIŞIK yığılmayacak şekilde interleave eder."""
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


# ========================================================================
# ADIM (Fixture Domestic/International Distribution Bug Fix) - eski
# mantık (`idx < n_dep_intl`, saat-artan sırayla YÜRÜYEN TEK global
# indeks) günün İLK yüzdesini (n_dep_intl'e kadar) TAMAMEN international,
# KALANINI TAMAMEN domestic yapıyordu - IST'de saat 17'ye kadar HER
# kalkış international, AMS'de neredeyse GÜNÜN TAMAMI international,
# SAW'da ilk yarı international/ikinci yarı domestic gibi gerçek dışı
# "blok" bir dağılım üretiyordu (bkz. rapor - "Passport Peak Demand
# Realism Audit"). Bu artık HER SAATİN KENDİ int'l/domestic oranını
# (`intl_ratio`) KENDİ flight count'u üzerinden uygulayıp saat İÇİNDE
# deterministic interleave etmesiyle DÜZELTİLDİ - hiçbir saat artık
# "tamamen international" veya "tamamen domestic" bir blok DEĞİL
# (aksi hâlâ mümkün: bir saatin kendi flight count'u 1-2 gibi çok
# küçükse ve oran ekstrem ise - ör. tek uçuşluk bir saat - ama bu
# GERÇEK bir havalimanının da o saatte gerçekten tek uçuşu olabileceği
# anlamına gelir, YAPAY bir global-indeks kesişimi DEĞİLDİR).
# ========================================================================

def _hourly_international_counts(hour_counts: dict[int, int], intl_ratio: float) -> dict[int, int]:
    """
    Her saatin KENDİ flight count'una `intl_ratio`'yu uygular - GLOBAL
    bir prefix/kesim YOK, her saat kendi oranını bağımsız taşır.

    Conservation EXACT: `departure_show_up_events()` ile AYNI kümülatif-
    yuvarlama ilkesi (Bölüm 4 - "kümülatif hedefin YUVARLANMIŞ farkı")
    - `sum(sonuç.values()) == round(sum(hour_counts.values()) *
    intl_ratio)` HER ZAMAN tam sağlanır, ara saatlerin ayrı ayrı
    yuvarlanması TOPLAMDA kayıp/fazlalık üretemez. Saatler DETERMINISTIC
    sabit bir sırayla (artan saat) dolaşılır - bu sıra SADECE yuvarlama
    kalıntısının hangi saate düştüğünü belirler, ESKİ bug'ın aksine
    hiçbir saati "tamamen" bir tipe İTMEZ (her saat kendi count'unun
    ORANINI alır, count sıfır değilse ve oran 0 ile 1 arasındaysa o
    saat İKİ TİPTEN de pay alabilir).
    """
    cumulative_target = 0.0
    cumulative_rounded = 0
    result: dict[int, int] = {}
    for hour in sorted(hour_counts):
        count = hour_counts[hour]
        cumulative_target += count * intl_ratio
        new_rounded = round(cumulative_target)
        result[hour] = min(count, new_rounded - cumulative_rounded)
        cumulative_rounded += result[hour]
    return result


def _interleave_flags(count: int, true_count: int) -> list[bool]:
    """
    `count` pozisyon içinde `true_count` adet `True`'yu DETERMINISTIC
    (RANDOM YOK) olarak MÜMKÜN OLDUĞUNCA EŞİT ARALIKLI yerleştirir -
    "ilk N tanesi True, kalanı False" gibi bloklaşma YOK (Bölüm 3 -
    "aynı hour içindeki flights deterministic interleave olsun").
    """
    flags = [False] * count
    if count <= 0 or true_count <= 0:
        return flags
    if true_count >= count:
        return [True] * count
    step = count / true_count
    for k in range(true_count):
        idx = int(k * step)
        if idx >= count:
            idx = count - 1
        flags[idx] = True
    return flags


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


_GLOBAL_FLIGHT_NUM = [1000]


def _next_flight_num() -> int:
    _GLOBAL_FLIGHT_NUM[0] += 1
    return _GLOBAL_FLIGHT_NUM[0]


def build_records_for_airport(airport: str) -> tuple[list[dict], list[dict], dict]:
    cfg = AIRPORTS[airport]
    scale = cfg["scale"]
    tz = ZoneInfo(cfg["tz"])

    n_total = cfg["target"]
    n_dep = n_total // 2
    n_arr = n_total - n_dep

    if n_total <= 40:
        dep_aircraft = _greedy_small_target_aircraft(n_dep, scale)
        arr_aircraft = _greedy_small_target_aircraft(n_arr, scale)
    else:
        dep_aircraft = _aircraft_cycle(n_dep, scale)
        arr_aircraft = _aircraft_cycle(n_arr, scale)

    dep_hours = _allocate_hours(n_dep, DEPARTURE_HOUR_WEIGHTS_BY_AIRPORT[airport])
    arr_hours = _allocate_hours(n_arr, ARR_HOUR_PROFILES[airport])

    intl_ratio = (1 - cfg["domestic_ratio"]) if cfg["intl"] else 0.0
    n_dep_intl = round(n_dep * intl_ratio)
    n_arr_intl = round(n_arr * intl_ratio)
    dep_hour_intl_counts = _hourly_international_counts(dep_hours, intl_ratio)
    arr_hour_intl_counts = _hourly_international_counts(arr_hours, intl_ratio)
    # Conservation EXACT doğrulaması (Bölüm 2) - saat-bazlı toplamlar
    # günlük hedefle BİREBİR eşleşmeli, ekstra/kayıp yolcu ÜRETİLMEMELİ.
    assert sum(dep_hour_intl_counts.values()) == n_dep_intl, airport
    assert sum(arr_hour_intl_counts.values()) == n_arr_intl, airport

    departures = []
    arrivals = []
    dep_demand = 0
    arr_demand = 0

    idx = 0
    for hour, count in sorted(dep_hours.items()):
        hour_flags = _interleave_flags(count, dep_hour_intl_counts.get(hour, 0))
        for i in range(count):
            minute = int(i * 60 / count) if count else 0
            local_dt = datetime(DATE.year, DATE.month, DATE.day, hour, minute, tzinfo=tz)
            dep_utc = local_dt.astimezone(UTC).replace(tzinfo=None)

            is_intl = hour_flags[i] and bool(cfg["intl"])
            if is_intl:
                partner = cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=8)
            else:
                partner = cfg["domestic"][idx % len(cfg["domestic"])] if cfg["domestic"] else cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["airlines"][idx % len(cfg["airlines"])] if cfg["airlines"] else cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=1, minutes=45)

            aircraft = dep_aircraft[idx] if idx < len(dep_aircraft) else dep_aircraft[-1]
            cap = _capacity_of(aircraft, scale)
            dep_demand += cap
            arr_utc = dep_utc + duration
            flight_num = _next_flight_num()
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
        hour_flags = _interleave_flags(count, arr_hour_intl_counts.get(hour, 0))
        for i in range(count):
            minute = int(i * 60 / count) if count else 0
            local_dt = datetime(DATE.year, DATE.month, DATE.day, hour, minute, tzinfo=tz)
            arr_utc = local_dt.astimezone(UTC).replace(tzinfo=None)

            is_intl = hour_flags[i] and bool(cfg["intl"])
            if is_intl:
                partner = cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=8)
            else:
                partner = cfg["domestic"][idx % len(cfg["domestic"])] if cfg["domestic"] else cfg["intl"][idx % len(cfg["intl"])]
                airline = cfg["airlines"][idx % len(cfg["airlines"])] if cfg["airlines"] else cfg["intl_airlines"][idx % len(cfg["intl_airlines"])]
                duration = timedelta(hours=1, minutes=45)

            aircraft = arr_aircraft[idx] if idx < len(arr_aircraft) else arr_aircraft[-1]
            cap = _capacity_of(aircraft, scale)
            arr_demand += cap
            dep_utc = arr_utc - duration
            flight_num = _next_flight_num()
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
        "airport": airport, "scale": scale, "research_target_movements": cfg["target"],
        "generated_movements": n_total, "departures": n_dep, "arrivals": n_arr,
        "modeled_passengers_day": dep_demand + arr_demand,
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
        diff_pct = (summary["generated_movements"] - summary["research_target_movements"]) / summary["research_target_movements"] * 100
        print(
            f"{airport}: research_movements={summary['research_target_movements']} "
            f"generated_movements={summary['generated_movements']} diff={diff_pct:+.1f}% "
            f"modeled_pax/day={summary['modeled_passengers_day']}"
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
    with open(OUT_DIR / "response-delays.json", "w", encoding="utf-8") as fh:
        json.dump(live_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    with open(OUT_DIR / "_generation_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summaries, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print()
    print(f"Total departures: {len(all_departures)}, arrivals: {len(all_arrivals)}, total: {len(all_departures) + len(all_arrivals)}")


if __name__ == "__main__":
    main()
