"""
AŞAMA 0 - Kaynak A ve Kaynak B'nin okunması ve normalize edilmesi.

Kaynak A : tarife verisi (arrivals / departures). Zaman, terminal,
           kapı, statü ve gecikme buradan gelir. aircraft_icao alanı
           çoğu kayıtta boştur.
Kaynak B : canlı uçuş verisi. aircraft_icao'nun asıl kaynağıdır.

Bu modül SAF ayrıştırma yapar: dosyayı okur, sözlüğe çevirir,
zaman alanlarını datetime'a dönüştürür. Veritabanına yazmaz.
"""

import json
import re
from datetime import datetime

from ...queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)

_NON_ALNUM = re.compile(r"[^A-Z0-9]")

# Kaynaklardaki zaman alanları "YYYY-MM-DD HH:MM" biçimindedir.
_TIME_FORMAT = "%Y-%m-%d %H:%M"


def normalize_flight_number(value: str | None) -> str | None:
    """
    Uçuş numarasını eşleştirilebilir hale getirir.

    "AC 72"  -> "AC72"
    "ac-72"  -> "AC72"
    None/""  -> None
    """
    if not value:
        return None
    cleaned = _NON_ALNUM.sub("", value.upper())
    return cleaned or None


def parse_utc(value: str | None) -> datetime | None:
    """Kaynaktaki UTC zaman metnini datetime'a çevirir."""
    if not value:
        return None
    try:
        return datetime.strptime(value, _TIME_FORMAT)
    except ValueError:
        return None


def load_source_payload(path: str) -> list[dict]:
    """
    Kaynak dosyasını okuyup kayıt listesini döndürür.

    Dosya {"response": [...]} sarmalayıcısıyla da, doğrudan liste
    olarak da gelebilir; ikisi de desteklenir.
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict):
        records = payload.get("response", [])
    else:
        records = payload

    return [r for r in records if isinstance(r, dict)]


def build_aircraft_index(source_b_records: list[dict]) -> dict[str, str]:
    """
    Kaynak B'den {normalize edilmiş uçuş no: aircraft_icao} indeksi.

    Uçak tipi olmayan kayıtlar indekse girmez - boş eşleşme
    üretmenin anlamı yok.
    """
    index: dict[str, str] = {}
    for record in source_b_records:
        icao = (record.get("aircraft_icao") or "").strip().upper()
        if not icao:
            continue
        for field in ("flight_iata", "flight_icao"):
            key = normalize_flight_number(record.get(field))
            if key and key not in index:
                index[key] = icao
    return index


def build_flight_key(
    airline_iata: str | None,
    flight_number: str | None,
    dep_scheduled_utc: datetime | None,
) -> str:
    """
    "{airline_iata}_{flight_number}_{dep_scheduled_date_utc}"

    Aynı uçuş tekrar geldiğinde aynı anahtarı üretir; böylece her
    refresh'te yeni satır açılmaz (YASAK 4).
    """
    airline = (airline_iata or "UNK").upper()
    number = flight_number or "UNK"
    date_part = (
        dep_scheduled_utc.date().isoformat() if dep_scheduled_utc else "UNKDATE"
    )
    return f"{airline}_{number}_{date_part}"


def resolve_location(
    dep_iata: str | None,
    arr_iata: str | None,
    country_by_iata: dict[str, str],
) -> str:
    """
    Kalkış ve varış aynı ülkedeyse domestic, değilse international.

    Ülke bilgisi bulunamıyorsa international varsayılır: passport
    yükünü eksik saymak, fazla saymaktan daha risklidir.
    """
    dep_country = country_by_iata.get((dep_iata or "").upper())
    arr_country = country_by_iata.get((arr_iata or "").upper())

    if dep_country and arr_country and dep_country == arr_country:
        return LOCATION_DOMESTIC
    return LOCATION_INTERNATIONAL


def parse_source_a_record(
    record: dict,
    direction: str,
    country_by_iata: dict[str, str],
    aircraft_index: dict[str, str] | None = None,
) -> dict | None:
    """
    Kaynak A kaydını Flight alanlarına eşler ve Kaynak B ile
    zenginleştirir.

    aircraft_icao önceliği:
      1) Kaynak A'nın kendi alanı (doluysa)
      2) Kaynak B eşleşmesi
      3) None - uydurma değer ÜRETİLMEZ
    """
    dep_iata = (record.get("dep_iata") or "").upper() or None
    arr_iata = (record.get("arr_iata") or "").upper() or None

    airport_iata = dep_iata if direction == DIRECTION_DEPARTURE else arr_iata
    if not airport_iata:
        return None

    dep_scheduled = parse_utc(record.get("dep_time_utc"))
    airline_iata = (record.get("airline_iata") or "").upper() or None
    flight_number = record.get("flight_number")

    own_icao = (record.get("aircraft_icao") or "").strip().upper() or None
    matched_icao = None
    if own_icao is None and aircraft_index:
        for field in ("flight_iata", "flight_icao"):
            key = normalize_flight_number(record.get(field))
            if key and key in aircraft_index:
                matched_icao = aircraft_index[key]
                break

    aircraft_icao = own_icao or matched_icao

    return {
        "flight_key": build_flight_key(airline_iata, flight_number, dep_scheduled),
        "airport_iata": airport_iata,
        "direction": direction,
        "location": resolve_location(dep_iata, arr_iata, country_by_iata),
        "airline_iata": airline_iata,
        "flight_number": flight_number,
        "flight_iata": normalize_flight_number(record.get("flight_iata")),
        "aircraft_icao": aircraft_icao,
        "aircraft_match_found": aircraft_icao is not None,
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "dep_scheduled_utc": dep_scheduled,
        "dep_estimated_utc": parse_utc(record.get("dep_estimated_utc")),
        "dep_actual_utc": parse_utc(record.get("dep_actual_utc")),
        "arr_scheduled_utc": parse_utc(record.get("arr_time_utc")),
        "arr_estimated_utc": parse_utc(record.get("arr_estimated_utc")),
        "arr_actual_utc": parse_utc(record.get("arr_actual_utc")),
        "dep_terminal": record.get("dep_terminal"),
        "dep_gate": record.get("dep_gate"),
        "arr_terminal": record.get("arr_terminal"),
        "arr_gate": record.get("arr_gate"),
        "status": (record.get("status") or "unknown").lower(),
    }


def parse_source_a(
    records: list[dict],
    direction: str,
    country_by_iata: dict[str, str],
    aircraft_index: dict[str, str] | None = None,
) -> list[dict]:
    """Kaynak A kayıt listesini Flight sözlüklerine çevirir."""
    parsed = []
    for record in records:
        row = parse_source_a_record(
            record, direction, country_by_iata, aircraft_index
        )
        if row is not None:
            parsed.append(row)
    return parsed


def aircraft_match_rate(rows: list[dict]) -> float:
    """
    Uçak tipi çözülebilen uçuşların oranı (AŞAMA 7 confidence girdisi).
    Hiç uçuş yoksa 0.0 - sahte %100 üretilmez.
    """
    if not rows:
        return 0.0
    matched = sum(1 for r in rows if r.get("aircraft_match_found"))
    return matched / len(rows)


__all__ = [
    "DIRECTION_ARRIVAL",
    "DIRECTION_DEPARTURE",
    "aircraft_match_rate",
    "build_aircraft_index",
    "build_flight_key",
    "load_source_payload",
    "normalize_flight_number",
    "parse_source_a",
    "parse_source_a_record",
    "parse_utc",
    "resolve_location",
]
