"""
Local/dev generated-source builder: converts raw departure/arrival board
dumps (tab-separated, as scraped) into the existing production Source-A
JSON shape and writes them to the SAME generated_delays_*.json paths the
pipeline already knows about (see pipeline.py's QUEUE_LOCAL_SOURCE_MODE
gate). Does not touch the default production source files
("Delays - Type *.json") or the default DB.

Usage:
    python scripts/generate_local_ist_source.py

Handles MULTIPLE home airports in one pass (originally IST-only; ADIM
"Generated Local Source Genişletme" extended it to AMS/DXB/TZX using
the same mechanism - see AIRPORTS below). Every home airport's own
departure board (own perspective, direction=departure) and own arrival
board (native arrival perspective, direction=arrival) are converted and
merged into the SAME two output files. "Preserve existing data" is
satisfied by determinism: the same raw txt input always produces the
same output record (same hash-based aircraft_icao, same rebased date),
so re-running this script after adding new airports reproduces the
existing airports' records byte-for-byte while adding the new ones -
there is no separate read-merge-write step, and none is needed.

Writes:
    data/generated_delays_departures.json  (one row per home airport's
                                             own departures)
    data/generated_delays_arrivals.json    (one row per home airport's
                                             own NATIVE arrivals; see
                                             NOTE below on derived
                                             destination-arrival rows)
    data/generated_response_delays.json    (Source B / aircraft
                                             enrichment - empty; see
                                             AIRCRAFT ICAO note below)

NOTE on derived destination-arrival perspective (spec section 3/6):
    For a departure record HOME->DEST we would ideally also emit a
    DEST-arrival perspective row for the same physical flight. Doing
    that correctly requires a real arrival timestamp at DEST, which
    this data source does not provide (each board only has its OWN
    airport's scheduled/estimated times, no destination-side time and
    no flight-duration table). Per the explicit instruction not to
    invent a random/estimated duration, no derived destination-arrival
    rows are produced here - this is reported explicitly in the
    generator's own summary output, not silently skipped. (Some of
    these destination airports - AMS, DXB, TZX - now happen to have
    their OWN native arrival board wired in separately, which is a
    real board, not a derived one.)

NOTE on aircraft_icao (ADIM Generated Data Aircraft Realism, extended):
    Exactly one field is added beyond what the raw board provides:
    aircraft_icao, chosen deterministically (hashlib.md5, no randomness)
    from the SAME real reference pools every prior task used
    (data/yolcu_ucaklari.json + data/curated_fallback.json - the
    project's own AircraftCapacity seed source, not a new mapping file).
    Nothing else is fabricated: no resolved capacity is written into the
    fixture, no passenger demand is precomputed - `app/service.py`'s
    AircraftCapacityService resolves capacity from this aircraft_icao at
    pipeline-run time exactly as it would for any real Source-A record
    with its own aircraft_icao field already populated (the field()
    priority order in app/queue/ingestion/sources.py:
    parse_source_a_record already reads "1) Kaynak A'nın kendi alanı
    (doluysa)" before ever consulting Source B) - this generator does
    not bypass or duplicate that resolver logic.
"""
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(r"C:\Users\assistant\aircraft-capacity-py")
sys.path.insert(0, str(REPO))

# ADIM (Generated Local Source Genişletme) - her home airport kendi
# gerçek IANA timezone'unda okunur (board saatleri o havalimanının
# YEREL saatidir) - AMS'nin Avrupa DST'si (Eylül'de hâlâ yaz saati,
# UTC+2) burada ÖNEMLİ, hepsini Europe/Istanbul ile okumak AMS
# show-up/graph bucket'larını 1 saat KAYDIRIRDI. ICAO kodları (LTFM/
# EHAM/OMDB/LTCG) IATA<->ICAO'su herkesçe bilinen, sabit gerçek
# havalimanı kodlarıdır - proje-özel bir mapping İCAT EDİLMEDİ.
AIRPORTS = [
    {
        "home_iata": "IST", "home_icao": "LTFM", "tz": "Europe/Istanbul",
        "dep_file": "ist_departure_raw.txt", "dep_has_header": True,
        "arr_file": "ist_arrival_raw.txt", "arr_has_header": False,
    },
    {
        "home_iata": "AMS", "home_icao": "EHAM", "tz": "Europe/Amsterdam",
        "dep_file": "ams_departure_raw.txt", "dep_has_header": False,
        "arr_file": "ams_arrival_raw.txt", "arr_has_header": False,
    },
    {
        "home_iata": "DXB", "home_icao": "OMDB", "tz": "Asia/Dubai",
        "dep_file": "dxb_departure_raw.txt", "dep_has_header": False,
        "arr_file": "dxb_arrival_raw.txt", "arr_has_header": False,
    },
    {
        "home_iata": "TZX", "home_icao": "LTCG", "tz": "Europe/Istanbul",
        "dep_file": "tzx_departure_raw.txt", "dep_has_header": False,
        "arr_file": "tzx_arrival_raw.txt", "arr_has_header": False,
    },
]

# ADIM (Generated Local Source Genişletme - Ek Board) - bazı export'lar
# tek bir yön (SADECE departure VEYA SADECE arrival) ve/veya farklı bir
# sütun şemasıyla (Location kolonu YOK - kaynağın kendi filtresi zaten
# tüm dosyayı tek bir location'a sabitlemiş, ör. "IST Departure
# International" search'ü) geliyor. Bunlar AIRPORTS'taki "bir havalimanı
# = kendi dep+arr board çifti" varsayımına UYMUYOR - AYRI, EK bir liste.
# Mevcut havalimanının departures/native_arrivals listesine "üzerine
# ekle" (spec) edilir - flight_key çakışırsa (aynı uçuş zaten mevcut
# board'da varsa) dedupe aşaması (main() - "ilk görülen kazanır") mevcut
# kaydı KORUR, YENİ/eksik uçuşlar eklenir.
SUPPLEMENTAL_BOARDS = [
    {
        "home_iata": "IST", "home_icao": "LTFM", "tz": "Europe/Istanbul",
        "file": "ist_departure_intl_raw.txt", "direction": "departure",
        "has_header": False, "has_location_column": False,
        "fixed_location": "international",
    },
]

# ADIM (Generated Data Aircraft Realism) - `app/service.py`'nin resolver'ı
# HİÇ DEĞİŞTİRİLMEDİ; bu SADECE üretilen fixture kaydına gerçek bir
# aircraft_icao yazarak Katman 3'ü (aircraft_capacity - ICAO exact match)
# tetiklemesini sağlar. `airline_fleet_seat_config` tablosu bu projede
# HİÇ SEED EDİLMEMİŞ (boş) - yani Katman 1-2 zaten hiçbir zaman
# tetiklenmiyor, airline_iata capacity'yi ETKİLEMİYOR; asıl kaldıraç
# aircraft_icao. Sadece data/yolcu_ucaklari.json + data/curated_fallback.json
# (mevcut, gerçek proje referans dosyaları) KULLANILIR - yeni bir
# mapping dosyası İCAT EDİLMEZ. Genel havacılık (GA) tipleri bu iki
# dosyada YOKTUR (ikisi de "verified yolcu uçakları" kaynağıdır) - bu
# yüzden buradan seçilen HERHANGİ bir kod otomatik olarak
# counts_toward_passenger_total=True bir yolcu uçağıdır.
_NARROW_CATEGORY = "Medium/short range jet aircraft"
_WIDE_CATEGORY = "Long range/wide-body jet aircraft"


def _load_capacity_pools() -> tuple[list[str], list[str]]:
    with open(REPO / "data" / "yolcu_ucaklari.json", encoding="utf-8") as fh:
        verified = json.load(fh)
    with open(REPO / "data" / "curated_fallback.json", encoding="utf-8") as fh:
        fallback = json.load(fh)

    narrow = sorted({item["icao"].upper() for item in verified if item["category"] == _NARROW_CATEGORY})
    wide = sorted({item["icao"].upper() for item in verified if item["category"] == _WIDE_CATEGORY})
    # curated_fallback.json has no "category" field (bkz. app/seed.py -
    # sadece icao+capacity taşır); tamamı gerçekte wide-body long-range
    # tipler (A346/B778/B779 vb.) - manuel olarak wide havuza eklenir.
    wide = sorted(set(wide) | {item["icao"].upper() for item in fallback})
    return narrow, wide


def _deterministic_aircraft_icao(key: str, location: str, narrow: list[str], wide: list[str]) -> str:
    """
    Aynı `key` HER ZAMAN aynı ICAO kodunu üretir (RANDOM YOK,
    `hashlib` kullanılır - Python'un yerleşik `hash()` process başına
    tuzlanır, deterministic DEĞİLDİR).

    domestic  : SADECE narrow-body havuzundan (Türkiye içi hatlar
                gerçekte hiç wide-body uçmaz).
    international : narrow-body ağırlıklı bir karışım (gerçek network'te
                uluslararası seferlerin büyük çoğunluğu da kısa/orta
                menzilli hatlardır, wide-body sadece azınlık uzun
                menzilli hatlarda) - digest'in son byte'ı mod 4 == 0
                (yaklaşık %25) ise wide-body havuzundan, aksi halde
                narrow'dan seçilir.
    """
    digest = hashlib.md5(key.encode("utf-8")).digest()
    if location == "domestic":
        pool = narrow
    else:
        pool = wide if digest[-1] % 4 == 0 else narrow
    index = int.from_bytes(digest[:4], "big") % len(pool)
    return pool[index]


_CITY_CODE_RE = re.compile(r"\(([A-Za-z0-9]{2,4})\)\s*$")
_STATUS_MAP = {
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "diverted": "diverted",
    "landed": "landed",
    "active": "active",
    "scheduled": "scheduled",
}


def _extract_code(city_field: str) -> str | None:
    m = _CITY_CODE_RE.search(city_field.strip())
    return m.group(1).upper() if m else None


def _split_airline_flight(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    parts = raw.split(None, 1)
    if len(parts) != 2:
        return "", ""
    airline_raw, number_raw = parts
    airline = re.sub(r"[^A-Z0-9]", "", airline_raw.upper())
    number = re.sub(r"[^0-9]", "", number_raw)
    return airline, number


def _parse_date(raw: str) -> date:
    dd, mm, yyyy = raw.strip().split("/")
    return date(int(yyyy), int(mm), int(dd))


def _local_to_utc_naive(day: date, hhmm: str, tz: ZoneInfo) -> datetime | None:
    hhmm = hhmm.strip()
    if not hhmm:
        return None
    try:
        hh, mm = hhmm.split(":")
        local_naive = datetime(day.year, day.month, day.day, int(hh), int(mm))
    except ValueError:
        return None
    local_aware = local_naive.replace(tzinfo=tz)
    utc_aware = local_aware.astimezone(ZoneInfo("UTC"))
    return utc_aware.replace(tzinfo=None)


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def _parse_board_file(path: Path, has_header: bool, has_location_column: bool = True) -> list[dict]:
    """
    `has_location_column=False` - bazı export'lar (ör. sitenin kendi
    "International" filtresiyle alınmış bir tarife) Location kolonunu
    HİÇ içermiyor - sütun sayısı 9 yerine 8 (Location'ın olduğu yerde
    doğrudan Status geliyor, Detail bir öncekine kayıyor). Bu durumda
    `location_raw` üretilmez - çağıran taraf (`build_records`) SABİT bir
    `fixed_location` vermek ZORUNDADIR (bkz. o fonksiyonun parametresi) -
    kaynağın KENDİ filtresi zaten location'ı search-time'da belirlemiş
    (ör. "IST Departure International" search'ü) - burada YENİDEN
    TAHMİN edilmez, kaynağın söylediği AYNEN kullanılır.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if has_header:
        lines = lines[1:]
    min_cols = 9 if has_location_column else 8
    rows = []
    for line in lines:
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < min_cols:
            continue
        row = {
            "airline_flight_raw": cols[0],
            "date_raw": cols[2],
            "scheduled_raw": cols[3],
            "estimated_raw": cols[4],
            "gate_raw": cols[5].strip(),
            "city_raw": cols[6],
        }
        if has_location_column:
            row["location_raw"] = cols[7].strip()
            row["status_raw"] = cols[8].strip()
        else:
            row["location_raw"] = None
            row["status_raw"] = cols[7].strip()
        rows.append(row)
    return rows


def build_records(rows_raw, is_departure_board, home_iata, home_icao, tz, today_local,
                   narrow_pool, wide_pool, fixed_location=None):
    out = []
    skipped_no_code = 0
    for r in rows_raw:
        airline, number = _split_airline_flight(r["airline_flight_raw"])
        if not airline and not number:
            skipped_no_code += 1
            continue
        code = _extract_code(r["city_raw"])
        if not code:
            skipped_no_code += 1
            continue
        row_date = _parse_date(r["date_raw"])
        delta_days = (today_local - row_date).days
        rebased_date = row_date + timedelta(days=delta_days)

        status = _STATUS_MAP.get(r["status_raw"].strip().lower(), r["status_raw"].strip().lower())
        if fixed_location is not None:
            location = fixed_location
        else:
            location = "domestic" if r["location_raw"].strip().lower() == "domestic" else "international"

        scheduled_utc = _local_to_utc_naive(rebased_date, r["scheduled_raw"], tz)
        estimated_utc = _local_to_utc_naive(rebased_date, r["estimated_raw"], tz)
        if estimated_utc is None:
            estimated_utc = scheduled_utc

        flight_iata = f"{airline}{number}" if airline and number else None

        aircraft_key = (
            f"{flight_iata or airline + number}_"
            f"{(home_iata + '_' + code) if is_departure_board else (code + '_' + home_iata)}_"
            f"{'departure' if is_departure_board else 'arrival'}"
        )
        aircraft_icao = _deterministic_aircraft_icao(aircraft_key, location, narrow_pool, wide_pool)

        if is_departure_board:
            rec = {
                "airline_iata": airline or None,
                "airline_icao": None,
                "flight_iata": flight_iata,
                "flight_icao": None,
                "flight_number": number or None,
                "dep_iata": home_iata,
                "dep_icao": home_icao,
                "dep_terminal": None,
                "dep_gate": r["gate_raw"] or None,
                "dep_time_utc": _fmt(scheduled_utc),
                "dep_estimated_utc": _fmt(estimated_utc),
                "dep_actual_utc": None,
                "arr_iata": code,
                "arr_icao": None,
                "arr_terminal": None,
                "arr_gate": None,
                "arr_time_utc": None,
                "arr_estimated_utc": None,
                "arr_actual_utc": None,
                "status": status,
                "aircraft_icao": aircraft_icao,
                "location": location,
                "direction": "departure",
            }
        else:
            rec = {
                "airline_iata": airline or None,
                "airline_icao": None,
                "flight_iata": flight_iata,
                "flight_icao": None,
                "flight_number": number or None,
                "dep_iata": code,
                "dep_icao": None,
                "dep_terminal": None,
                "dep_gate": None,
                "dep_time_utc": None,
                "dep_estimated_utc": None,
                "dep_actual_utc": None,
                "arr_iata": home_iata,
                "arr_icao": home_icao,
                "arr_terminal": None,
                "arr_gate": r["gate_raw"] or None,
                "arr_time_utc": _fmt(scheduled_utc),
                "arr_estimated_utc": _fmt(estimated_utc),
                "arr_actual_utc": None,
                "status": status,
                "aircraft_icao": aircraft_icao,
                "location": location,
                "direction": "arrival",
            }
        out.append(rec)
    return out, skipped_no_code


def main():
    narrow_pool, wide_pool = _load_capacity_pools()

    all_departures: list[dict] = []
    all_native_arrivals: list[dict] = []
    per_airport_summary = {}

    for cfg in AIRPORTS:
        tz = ZoneInfo(cfg["tz"])
        today_local = datetime.now(tz).date()

        dep_path = REPO / "data" / cfg["dep_file"]
        arr_path = REPO / "data" / cfg["arr_file"]
        dep_rows_raw = _parse_board_file(dep_path, has_header=cfg["dep_has_header"])
        arr_rows_raw = _parse_board_file(arr_path, has_header=cfg["arr_has_header"])

        departures, dep_skipped = build_records(
            dep_rows_raw, True, cfg["home_iata"], cfg["home_icao"], tz, today_local,
            narrow_pool, wide_pool,
        )
        native_arrivals, arr_skipped = build_records(
            arr_rows_raw, False, cfg["home_iata"], cfg["home_icao"], tz, today_local,
            narrow_pool, wide_pool,
        )

        all_departures.extend(departures)
        all_native_arrivals.extend(native_arrivals)

        dom_dep = sum(1 for r in departures if r["location"] == "domestic")
        intl_dep = sum(1 for r in departures if r["location"] == "international")
        dom_arr = sum(1 for r in native_arrivals if r["location"] == "domestic")
        intl_arr = sum(1 for r in native_arrivals if r["location"] == "international")

        per_airport_summary[cfg["home_iata"]] = {
            "today_local": str(today_local),
            "departure_input_rows": len(dep_rows_raw),
            "arrival_input_rows": len(arr_rows_raw),
            "departures_generated": len(departures),
            "departures_skipped_no_code": dep_skipped,
            "native_arrivals_generated": len(native_arrivals),
            "native_arrivals_skipped_no_code": arr_skipped,
            "dep": {"domestic": dom_dep, "international": intl_dep, "total": len(departures)},
            "arr": {"domestic": dom_arr, "international": intl_arr, "total": len(native_arrivals)},
        }

    supplemental_summary = {}
    for sup in SUPPLEMENTAL_BOARDS:
        tz = ZoneInfo(sup["tz"])
        today_local = datetime.now(tz).date()
        path = REPO / "data" / sup["file"]
        rows_raw = _parse_board_file(
            path, has_header=sup["has_header"], has_location_column=sup["has_location_column"],
        )
        is_dep = sup["direction"] == "departure"
        records, skipped = build_records(
            rows_raw, is_dep, sup["home_iata"], sup["home_icao"], tz, today_local,
            narrow_pool, wide_pool, fixed_location=sup["fixed_location"],
        )
        if is_dep:
            all_departures.extend(records)
        else:
            all_native_arrivals.extend(records)
        supplemental_summary[sup["file"]] = {
            "home_iata": sup["home_iata"], "direction": sup["direction"],
            "fixed_location": sup["fixed_location"],
            "input_rows": len(rows_raw), "records_generated": len(records),
            "skipped_no_code": skipped,
        }

    # Derived destination-arrival perspective: NOT produced (see module
    # docstring) - none of these boards carries a destination-side
    # arrival timestamp or flight-duration table for the OTHER leg, and
    # inventing one would be a fabricated random duration.
    derived_arrivals: list[dict] = []
    arrivals_skipped_no_reliable_time = len(all_departures)

    # Native + derived dedupe (spec section 4) - matches on normalized
    # (airline, flight_number, dep_iata, arr_iata, first 10 chars of the
    # relevant timestamp). No-op today since derived_arrivals is empty,
    # kept for correctness if a future source supplies durations.
    def dedupe_key(rec):
        return (
            (rec.get("airline_iata") or "").upper(),
            (rec.get("flight_number") or "").upper(),
            (rec.get("dep_iata") or "").upper(),
            (rec.get("arr_iata") or "").upper(),
            (rec.get("arr_time_utc") or rec.get("dep_time_utc") or "")[:10],
        )

    native_keys = {dedupe_key(r) for r in all_native_arrivals}
    derived_arrivals_deduped = [r for r in derived_arrivals if dedupe_key(r) not in native_keys]
    derived_dropped_as_duplicate = len(derived_arrivals) - len(derived_arrivals_deduped)

    all_arrivals = all_native_arrivals + derived_arrivals_deduped

    # Global duplicate safety net across ALL airports/records combined -
    # should be a no-op (different home_iata means different dep_iata/
    # arr_iata pairs -> different keys) but checked explicitly rather
    # than assumed.
    dep_keys_seen = set()
    dep_dupes_dropped = 0
    deduped_departures = []
    for rec in all_departures:
        k = dedupe_key(rec)
        if k in dep_keys_seen:
            dep_dupes_dropped += 1
            continue
        dep_keys_seen.add(k)
        deduped_departures.append(rec)

    arr_keys_seen = set()
    arr_dupes_dropped = 0
    deduped_arrivals = []
    for rec in all_arrivals:
        k = dedupe_key(rec)
        if k in arr_keys_seen:
            arr_dupes_dropped += 1
            continue
        arr_keys_seen.add(k)
        deduped_arrivals.append(rec)

    data_dir = REPO / "data"
    with open(data_dir / "generated_delays_departures.json", "w", encoding="utf-8") as fh:
        json.dump(deduped_departures, fh, indent=2, ensure_ascii=False)
    with open(data_dir / "generated_delays_arrivals.json", "w", encoding="utf-8") as fh:
        json.dump(deduped_arrivals, fh, indent=2, ensure_ascii=False)
    with open(data_dir / "generated_response_delays.json", "w", encoding="utf-8") as fh:
        json.dump([], fh, indent=2, ensure_ascii=False)

    all_records = deduped_departures + deduped_arrivals
    aircraft_non_null = sum(1 for r in all_records if r["aircraft_icao"])
    used_codes = {r["aircraft_icao"] for r in all_records if r["aircraft_icao"]}

    summary = {
        "airports": list(per_airport_summary.keys()),
        "per_airport": per_airport_summary,
        "supplemental_boards": supplemental_summary,
        "totals": {
            "departures": len(deduped_departures),
            "native_arrivals": len(all_native_arrivals),
            "derived_destination_arrivals_generated": len(derived_arrivals_deduped),
            "derived_destination_arrivals_dropped_as_duplicate": derived_dropped_as_duplicate,
            "arrivals_skipped_no_reliable_time": arrivals_skipped_no_reliable_time,
            "departure_duplicates_dropped": dep_dupes_dropped,
            "arrival_duplicates_dropped": arr_dupes_dropped,
        },
        "aircraft_icao_assignment": {
            "narrow_pool_size": len(narrow_pool),
            "wide_pool_size": len(wide_pool),
            "total_records": len(all_records),
            "aircraft_icao_non_null": aircraft_non_null,
            "aircraft_icao_null": len(all_records) - aircraft_non_null,
            "distinct_icao_codes_used": len(used_codes),
        },
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
