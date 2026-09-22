"""
ADIM (Live-Like Test Data Ingestion: AMS/IST/SAW/TZX) - `data/`'daki
gerçek/aktif departure board tablolarını (ams_live_test.txt, ist_live_
test.txt, saw_live_test.txt, tzx_live_text.txt - SON DOSYA ADI
KASITLI DEĞİŞTİRİLMEDİ, gerçek dosya adı "text" yazıyor, "test" değil,
bkz. rapor) mevcut production source JSON şemasına (generate_fixture.py
İLE AYNI alan adları/nesting - o da zaten production parser'la uyumlu
KANITLANMIŞ) çevirip bu 4 havalimanının eski SYNTHETIC kayıtlarının
YERİNE koyar.

SADECE bu klasördeki (`tests/realistic_9_airports_2026_09_22/`) JSON
dosyalarını GÜNCELLER - gerçek `data/Delays - Type *.json`/production
`database.sqlite` HİÇ açılmaz/yazılmaz. Production parser (`app/queue/
ingestion/sources.py`) DEĞİŞTİRİLMEDİ.

DIRECTION: her dosya kendi havalimanının DEPARTURE board'udur - "Arrival"
kolonu DESTINATION'dır (kullanıcının açık düzeltmesi, bkz. rapor).
Domestic/International kolonu SOURCE OF TRUTH - fixture ratio/generator
ile YENİDEN ÜRETİLMEZ.

CROSS-LINKING: destination bu 4 havalimanından biriyse (AMS/IST/SAW/TZX)
aynı fiziksel uçuş için AYNI flight identity ile bir ARRIVAL kaydı da
üretilir (departure ile arrival AYNI flight_iata/flight_number - iki kez
sayılmaz, bkz. `test_source_shape` conservation kontrolü).
"""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(r"C:\Users\assistant\aircraft-capacity-py")
sys.path.insert(0, str(REPO))

import json  # noqa: E402

from app.queue.ingestion.airports_import import parse_airports_sql  # noqa: E402

DATA_DIR = REPO / "data"
FIXTURE_DIR = Path(__file__).resolve().parent
DATE = datetime(2026, 9, 22)
UTC = ZoneInfo("UTC")

# Gerçek dosya adları - kullanıcının verdiği isimlerle BİREBİR AYNI DEĞİL
# (TZX dosyası "tzx_live_text.txt" - "test" değil "text" - bariz bir
# yazım farkı, tek aday dosya, otomatik TAHMİN edilmedi, açıkça TESPİT
# edildi - bkz. final rapor).
SOURCE_FILES = {
    "AMS": DATA_DIR / "ams_live_test.txt",
    "IST": DATA_DIR / "ist_live_test.txt",
    "SAW": DATA_DIR / "saw_live_test.txt",
    "TZX": DATA_DIR / "tzx_live_text.txt",
    "DUB": DATA_DIR / "dub_live_test.txt",
}

AIRPORT_TZ = {
    "AMS": ZoneInfo("Europe/Amsterdam"),
    "IST": ZoneInfo("Europe/Istanbul"),
    "SAW": ZoneInfo("Europe/Istanbul"),
    "TZX": ZoneInfo("Europe/Istanbul"),
    "DUB": ZoneInfo("Europe/Dublin"),
}
AIRPORT_ICAO = {"AMS": "EHAM", "IST": "LTFM", "SAW": "LTFJ", "TZX": "LTCG", "DUB": "EIDW"}
TRACKED = set(AIRPORT_TZ)

# ========================================================================
# Aircraft/duration sınıflandırması - GERÇEK koordinat verisi repo'da
# YOK (Airport modelinde lat/lon alanı yok) - bu yüzden distance-based
# hesap yerine, destination'ın coğrafi bölgesine göre deterministic
# bir kategori + sabit süre bandı kullanılıyor (kullanıcının kendi
# talimatı: "amaç birebir fleet reconstruction değil"). Kategoriler
# `data/yolcu_ucaklari.json`'da VERIFIED confidence ile bulunan ICAO
# tiplerinden seçildi (B77W hariç - o family-prefix fallback'e düşüyor,
# kullanıcının kendi örnek listesinde AÇIKÇA istendiği için KORUNDU).
# ========================================================================
REGIONAL_POOL = ("E190", "E195", "A319")
SHORT_MEDIUM_POOL = ("A320", "A321", "B738", "B739")
MEDIUM_LONG_POOL = ("A333", "A359", "B789")
LONG_HAUL_POOL = ("A333", "A359", "B789", "B77W")

REGIONAL_MINUTES = 70
SHORT_MEDIUM_MINUTES = 150
MEDIUM_LONG_MINUTES = 270
LONG_HAUL_MINUTES = 480

# Türkiye iç hat / TZX-IST-SAW besleme (REGIONAL - E-jet/A319 tipik).
TURKEY_DOMESTIC_CODES = {
    "ADF", "ADB", "AJI", "ASR", "AYT", "BAL", "BJV", "DIY", "DLM", "DNZ",
    "EDO", "ERC", "ERZ", "ESB", "EZS", "GNY", "GZP", "GZT", "HTY", "IGD",
    "KCM", "KSY", "KYA", "MLX", "MQM", "MSR", "NAV", "OGU", "SZF", "TJK",
    "TZX", "VAN", "VAS", "IST", "SAW",
}

# Kuzey Amerika / Güney Amerika / Karayip / Asya-Pasifik / Sahra-altı
# Afrika - LONG_HAUL (widebody, ~8sa).
LONG_HAUL_CODES = {
    # Kuzey Amerika
    "ATL", "BDL", "BNA", "BOS", "CLT", "DEN", "DFW", "DTW", "EWR", "IAD",
    "IAH", "JFK", "LAX", "MCO", "MIA", "MSP", "ORD", "PDX", "PHL", "RDU",
    "SAN", "SEA", "SFO", "SLC",
    "YHZ", "YUL", "YVR", "YYC", "YYZ",
    # Guney Amerika / Karayip
    "BOG", "GIG", "GRU", "LIM", "PTY", "SJO", "UIO", "BON", "MBJ", "SXM",
    # Asya-Pasifik / Guney Asya
    "BKK", "BLR", "BOM", "CMB", "DAC", "DEL", "HKG", "HYD", "ICN", "KHI",
    "KUL", "NRT", "PEK", "PKX", "PVG", "SIN", "TFU", "TPE",
    # Sahra-alti Afrika
    "ABV", "ACC", "ADD", "ASM", "CPT", "DAR", "DLA", "JNB", "JRO", "KGL",
    "LOS", "NBO", "NDJ", "NKC", "ZNZ", "COO",
}

# Korfez / Levant / Iran - MEDIUM_LONG (widebody-capable ama daha kisa
# sure, ~4.5sa - Turkish/Korfez tasiyicilarin gercek pratiginin kaba
# yaklasimi).
MEDIUM_LONG_CODES = {
    "AUH", "BAH", "DMM", "DOH", "DXB", "JED", "KWI", "MED", "RUH", "SHJ",
    "IKA", "THR", "MHD", "BEY", "AMM", "DAM", "TLV",
}

# Kalan HER ŞEY (Avrupa, Kuzey Afrika Akdeniz kiyisi, Rusya/Kafkasya/
# Orta Asya, Balkanlar) - SHORT_MEDIUM (varsayilan, narrowbody).


def _classify(dest_iata: str, is_domestic: bool) -> tuple[tuple[str, ...], int]:
    """
    ADIM (Classification Bug Fix) - `TURKEY_DOMESTIC_CODES` SADECE
    `is_domestic=True` iken uygulanir. Ilk versiyon bunu `is_domestic`'ten
    BAGIMSIZ kontrol ediyordu - bu, AMS->IST/AMS->SAW gibi GERCEKTEN
    international (Hollanda->Turkiye, ~3.5sa) ucuslari, hedef IST/SAW
    oldugu icin YANLISLIKLA REGIONAL (E190/A319, 70dk) kategorisine
    dusuruyordu (bkz. rapor - "AMS Cross-Airport Mapping" denetiminde
    bulundu: TK1952/KL1959/VF2/PC1252). `is_domestic` bayragi SOURCE
    OF TRUTH (input'un kendi Location kolonu) - TURKEY_DOMESTIC_CODES
    SADECE input zaten "Domestic" dediginde bir anlam ifade eder.
    """
    if is_domestic and dest_iata in TURKEY_DOMESTIC_CODES:
        return REGIONAL_POOL, REGIONAL_MINUTES
    if dest_iata in LONG_HAUL_CODES:
        return LONG_HAUL_POOL, LONG_HAUL_MINUTES
    if dest_iata in MEDIUM_LONG_CODES:
        return MEDIUM_LONG_POOL, MEDIUM_LONG_MINUTES
    return SHORT_MEDIUM_POOL, SHORT_MEDIUM_MINUTES


def _deterministic_pick(pool: tuple[str, ...], *key_parts: str) -> str:
    """RANDOM YOK - stabil hash'ten deterministic indeks (Bolum 6)."""
    key = "|".join(key_parts).encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    idx = int(digest[:8], 16) % len(pool)
    return pool[idx]


_ARRIVAL_RE = re.compile(r"^(.*)\(([A-Za-z0-9]{2,4})\)\s*$")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_STATUS_MAP = {
    "scheduled": "scheduled",
    "active": "active",
    "landed": "landed",
    "canceled": "cancelled",  # ADIM: Amerikan yaziminin ("canceled")
    # production'in bekledigi Ingiliz yazimina ("cancelled",
    # constants.py:EXCLUDED_STATUSES) NORMALIZE edilmesi - status'un
    # KENDISI/ANLAMI DEGISTIRILMEDI, SADECE mevcut, degismeyen exclusion
    # kontrolunun (STATUS_CANCELLED="cancelled") dogru eslesmesi icin
    # yazim birlestirildi. "canceled" AYNEN birakilsaydi mevcut
    # production kodu (degistirilmedi) bu ucuslari YANLISLIKLA demand'e
    # DAHIL ederdi - bu, "status'u degistirme" talimatinin degil,
    # "canceled flights demand disi kalmali" talimatinin (Bolum 4)
    # dogrudan gerektirdigi bir duzeltmedir.
    "cancelled": "cancelled",
    "diverted": "diverted",
}


def _parse_file(airport: str, path: Path) -> list[dict]:
    """Tek bir departure-board dosyasini ayristirir (SOURCE OF TRUTH)."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            cols = line.split("\t")
            if cols[0].strip().lower() == "airline":
                continue
            # ADIM (DUB Robustness Fix) - DUB dosyasinda bazi satirlarda
            # "Airline FlightNo" alani BIR KEZ, cogu satirda IKI KEZ
            # (duplicate) geciyor - sabit kolon index'i (cols[2]) yerine
            # tarih alanini (DD/MM/YYYY) REGEX ile bulup ondan SONRAKI
            # alanlari (sched/est/gate/arrival/location/status) ondan
            # GORECELI konumlandiriyoruz. AMS/IST/SAW/TZX'te tarih HER
            # ZAMAN cols[2]'de oldugundan davranis DEGISMEZ.
            date_idx = next((i for i, c in enumerate(cols) if _DATE_RE.match(c.strip())), None)
            if date_idx is None or date_idx == 0 or len(cols) < date_idx + 7:
                continue
            airline_raw = cols[0]
            sched_raw, est_raw, gate_raw, arrival_raw, location_raw, status_raw = cols[date_idx + 1:date_idx + 7]

            m = _ARRIVAL_RE.match(arrival_raw.strip())
            if not m:
                continue
            dest_city, dest_iata = m.group(1).strip(), m.group(2).strip().upper()

            location = location_raw.strip().lower()
            if location not in ("domestic", "international"):
                continue

            status_key = status_raw.strip().lower()
            status = _STATUS_MAP.get(status_key, status_key)

            airline_iata = airline_raw.strip().split(" ")[0]
            flight_num_match = re.search(r"(\d+)\s*$", airline_raw.strip())
            flight_num = flight_num_match.group(1) if flight_num_match else airline_raw.strip()
            flight_iata = f"{airline_iata}{flight_num}"

            try:
                sched_h, sched_m = (int(x) for x in sched_raw.strip().split(":"))
            except ValueError:
                continue
            local_dep = datetime(DATE.year, DATE.month, DATE.day, sched_h, sched_m, tzinfo=AIRPORT_TZ[airport])

            est_dt = None
            est_raw = est_raw.strip()
            if est_raw:
                try:
                    est_h, est_m = (int(x) for x in est_raw.split(":"))
                    est_dt = datetime(DATE.year, DATE.month, DATE.day, est_h, est_m, tzinfo=AIRPORT_TZ[airport])
                    # Estimated saat < scheduled saat ve fark buyukse
                    # (ornek 23:50 scheduled -> 00:05 estimated) bir
                    # sonraki takvim gunune GECMIS say - GECE YARISI
                    # SINIRINI dogru gecebilmek icin (Bolum 25 - cross-
                    # midnight destek).
                    if (est_dt - local_dep).total_seconds() < -12 * 3600:
                        est_dt += timedelta(days=1)
                    elif (est_dt - local_dep).total_seconds() > 12 * 3600:
                        est_dt -= timedelta(days=1)
                except ValueError:
                    est_dt = None

            rows.append({
                "airport": airport,
                "airline_iata": airline_iata,
                "flight_iata": flight_iata,
                "flight_number": flight_num,
                "dest_city": dest_city,
                "dest_iata": dest_iata,
                "location": location,
                "status": status,
                "sched_local": local_dep,
                "est_local": est_dt,
                "gate": gate_raw.strip() or None,
            })
    return rows


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _airport_icao_lookup() -> dict[str, str]:
    """Gercek data/flight_airports.sql'den IATA->ICAO (destination'lar icin)."""
    rows = parse_airports_sql(str(DATA_DIR / "flight_airports.sql"))
    return {r["iata_code"].upper(): r["icao_code"] for r in rows if r.get("icao_code")}


def _placeholder_icao(iata: str) -> str:
    """Bilinmeyen IATA icin deterministic 4-harfli placeholder (RANDOM YOK)."""
    digest = hashlib.sha256(iata.encode("utf-8")).hexdigest()
    return "ZZ" + digest[:2].upper()


def build_records():
    airport_icao = _airport_icao_lookup()
    for code, icao in AIRPORT_ICAO.items():
        airport_icao[code] = icao  # 4 tracked airport icin ACIKCA verilen ICAO oncelikli

    all_rows: dict[str, list[dict]] = {}
    for code, path in SOURCE_FILES.items():
        if not path.exists():
            raise FileNotFoundError(f"{code}: source file not found: {path}")
        all_rows[code] = _parse_file(code, path)

    departures: list[dict] = []
    arrivals: list[dict] = []
    seen_arrival_keys: set[tuple] = set()

    stats = {code: {
        "source_rows": 0, "domestic": 0, "international": 0, "cancelled": 0,
        "derived_arrivals_in": 0,
    } for code in SOURCE_FILES}

    for code, rows in all_rows.items():
        for row in rows:
            stats[code]["source_rows"] += 1
            if row["location"] == "domestic":
                stats[code]["domestic"] += 1
            else:
                stats[code]["international"] += 1
            if row["status"] == "cancelled":
                stats[code]["cancelled"] += 1

            dep_airport = row["airport"]
            dest_iata = row["dest_iata"]
            # Siniflandirma SADECE coğrafyaya (destination) bagli -
            # domestic/international bayragindan BAGIMSIZ (Bolum 6 -
            # "route/distance/airline-context based").
            duration_pool, duration_minutes = _classify(dest_iata, row["location"] == "domestic")
            aircraft_icao = _deterministic_pick(duration_pool, row["flight_iata"], dep_airport, dest_iata)
            duration = timedelta(minutes=duration_minutes)

            dep_effective_local = row["est_local"] or row["sched_local"]
            dep_utc = dep_effective_local.astimezone(UTC).replace(tzinfo=None)
            sched_utc = row["sched_local"].astimezone(UTC).replace(tzinfo=None)
            est_utc = row["est_local"].astimezone(UTC).replace(tzinfo=None) if row["est_local"] else None

            arr_utc_derived = dep_utc + duration

            dest_icao = airport_icao.get(dest_iata) or _placeholder_icao(dest_iata)
            dep_icao = AIRPORT_ICAO[dep_airport]

            base_record = {
                "airline_iata": row["airline_iata"], "airline_icao": None,
                "flight_iata": row["flight_iata"], "flight_icao": None,
                "flight_number": row["flight_number"],
                "dep_iata": dep_airport, "dep_icao": dep_icao,
                "dep_terminal": None, "dep_gate": row["gate"],
                "dep_time": _fmt(row["sched_local"].replace(tzinfo=None)),
                "dep_time_utc": _fmt(sched_utc),
                "dep_estimated": _fmt((row["est_local"] or row["sched_local"]).replace(tzinfo=None)),
                "dep_estimated_utc": _fmt(est_utc if est_utc else sched_utc),
                "dep_actual": None, "dep_actual_utc": None,
                "arr_iata": dest_iata, "arr_icao": dest_icao,
                "arr_terminal": None, "arr_gate": None, "arr_baggage": None,
                "arr_time": _fmt(arr_utc_derived), "arr_time_utc": _fmt(arr_utc_derived),
                "arr_estimated": _fmt(arr_utc_derived), "arr_estimated_utc": _fmt(arr_utc_derived),
                "cs_airline_iata": None, "cs_flight_number": None, "cs_flight_iata": None,
                "status": row["status"],
                "duration": duration_minutes,
                "delayed": 0, "dep_delayed": 0, "arr_delayed": 0,
                "aircraft_icao": aircraft_icao,
                "location": row["location"],
                "arr_time_ts": int(arr_utc_derived.timestamp()), "dep_time_ts": int(dep_utc.timestamp()),
            }
            departures.append(base_record)

            if dest_iata in TRACKED and dest_iata != dep_airport:
                key = (row["airline_iata"], row["flight_number"], dep_airport, dest_iata)
                if key in seen_arrival_keys:
                    continue
                seen_arrival_keys.add(key)
                arr_record = dict(base_record)  # AYNI flight identity - duplicate demand degil (Bolum 10)
                arrivals.append(arr_record)
                stats[dest_iata]["derived_arrivals_in"] += 1

    return departures, arrivals, stats


def main():
    departures, arrivals, stats = build_records()

    dep_path = FIXTURE_DIR / "Delays - Type Departures.json"
    arr_path = FIXTURE_DIR / "Delays - Type Arrivals.json"

    with open(dep_path, encoding="utf-8") as fh:
        dep_payload = json.load(fh)
    with open(arr_path, encoding="utf-8") as fh:
        arr_payload = json.load(fh)

    before_dep = len(dep_payload["response"])
    before_arr = len(arr_payload["response"])

    # ADIM 17 - eski SYNTHETIC AMS/IST/SAW/TZX kayitlarini SIL (APPEND
    # YOK) - departure kayitlarinda dep_iata, arrival kayitlarinda
    # arr_iata bu 4 havalimanindan biriyse kaldirilir. Eski synthetic
    # fixture route partner'lari BILEREK bu 4 havalimaninin HICBIRI
    # DEGILDIR (generate_fixture.py docstring'i) - bu yuzden "diger
    # havalimanlarin AMS/IST/SAW/TZX'e giden eski synthetic ucuslari"
    # diye bir sey ZATEN YOK, sadece bu 4'un KENDI eski kayitlari silinir.
    dep_payload["response"] = [
        r for r in dep_payload["response"] if r.get("dep_iata") not in TRACKED
    ]
    arr_payload["response"] = [
        r for r in arr_payload["response"] if r.get("arr_iata") not in TRACKED
    ]
    removed_dep = before_dep - len(dep_payload["response"])
    removed_arr = before_arr - len(arr_payload["response"])

    dep_payload["response"].extend(departures)
    arr_payload["response"].extend(arrivals)

    with open(dep_path, "w", encoding="utf-8") as fh:
        json.dump(dep_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(arr_path, "w", encoding="utf-8") as fh:
        json.dump(arr_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    report = {
        "removed_old_synthetic_departures": removed_dep,
        "removed_old_synthetic_arrivals": removed_arr,
        "new_departures_added": len(departures),
        "new_arrivals_added": len(arrivals),
        "per_airport_source_stats": stats,
    }
    with open(FIXTURE_DIR / "live_ingestion_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
