"""
AŞAMA 0 - Kaynak A ve Kaynak B'nin okunması ve normalize edilmesi.

Kaynak A : tarife verisi (arrivals / departures). Zaman, terminal,
           kapı, statü ve gecikme buradan gelir. aircraft_icao alanı
           çoğu kayıtta boştur.
Kaynak B : canlı uçuş verisi. aircraft_icao'nun asıl kaynağıdır.

Bu modül SAF ayrıştırma yapar: kayıt sözlüğünü alır, alanları
normalize eder, zamanları datetime'a çevirir. Veritabanına yazmaz.

CANLI BESLEMEYE HAZIR: Ayrıştırıcı bir dosyaya değil, kayıt
LİSTESİNE bağlıdır (`parse_source_a`). Dosya okuma yalnızca
`load_source_payload` içindedir; canlı API bağlandığında o fonksiyon
yerine istek sonucu verilir, geri kalan katmanların hiçbiri değişmez.

Alan adları da tek bir şemaya bağlı değildir. Aynı bilgi hem
snake_case (`dep_time_utc`, `dep_iata`) hem camelCase
(`depScheduledUtc`, `depIata`) şemasıyla okunabilir; hangisi gelirse
gelsin aynı Flight alanlarına düşer. `direction` ve `location` kayıtta
HAZIR geliyorsa doğrudan kullanılır, tekrar hesaplanmaz.
"""

import json
import re
from datetime import datetime, timezone

from ...queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)

_NON_ALNUM = re.compile(r"[^A-Z0-9]")

# Kaynaklarda görülen zaman biçimleri. Canlı feed ISO 8601 de
# gönderebilir; hepsi naive UTC datetime'a indirgenir çünkü tüm iç
# hesaplamalar UTC üzerinden yapılır.
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)

# Ham metinde "yok" anlamına gelen gösterimler.
_NULL_TOKENS = {"", "-", "--", "n/a", "na", "null", "none", "unknown"}


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


def clean_text(value):
    """
    Ham alanı temizler. `"-"` gibi "yok" gösterimleri None'a döner,
    böylece sahte bir terminal/kapı/uçak tipi değeri üretilmez.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.lower() in _NULL_TOKENS:
        return None
    return stripped


def field(record: dict, *names):
    """
    Aynı bilginin farklı şemalardaki adlarını sırayla dener.

    Böylece yeni bir besleme geldiğinde ayrıştırıcıyı yeniden yazmak
    yerine buraya bir ad eklemek yeterli olur.
    """
    for name in names:
        value = clean_text(record.get(name))
        if value is not None:
            return value
    return None


def parse_utc(value) -> datetime | None:
    """
    UTC zaman metnini naive datetime'a çevirir.

    Bilinen biçimlerin hiçbiri tutmazsa ISO 8601 denenir ("...Z" veya
    "+03:00" ekli olabilir); saat dilimi bilgisi UTC'ye çevrilip
    düşürülür. Hiçbiri olmazsa None - uydurma zaman üretilmez.
    """
    value = clean_text(value)
    if not value:
        return None

    for time_format in _TIME_FORMATS:
        try:
            return datetime.strptime(value, time_format)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


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
        icao = (field(record, "aircraft_icao", "aircraftIcao") or "").upper()
        if not icao:
            continue
        for name in ("flight_iata", "flightIata", "flight_icao", "flightIcao"):
            key = normalize_flight_number(record.get(name))
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

    SADECE kayıtta `location` alanı YOKSA çalışır. Alan hazır geldiğinde
    ülke karşılaştırması gereksizdir ve yapılmaz.

    Ülke bilgisi bulunamıyorsa international varsayılır: passport
    yükünü eksik saymak, fazla saymaktan daha risklidir.
    """
    dep_country = country_by_iata.get((dep_iata or "").upper())
    arr_country = country_by_iata.get((arr_iata or "").upper())

    if dep_country and arr_country and dep_country == arr_country:
        return LOCATION_DOMESTIC
    return LOCATION_INTERNATIONAL


def read_direction(record: dict, default: str) -> str:
    """
    Kayıttaki `direction` hazırsa kullanılır, tekrar çıkarılmaz.
    Yoksa beslemenin kendi yönü (arrivals/departures dosyası) geçerlidir.
    """
    value = (field(record, "direction") or "").lower()
    if value in (DIRECTION_ARRIVAL, DIRECTION_DEPARTURE):
        return value
    return default


def read_location(
    record: dict, dep_iata: str | None, arr_iata: str | None,
    country_by_iata: dict[str, str],
) -> str:
    """Kayıttaki `location` hazırsa kullanılır; yoksa türetilir."""
    value = (field(record, "location") or "").lower()
    if value in (LOCATION_DOMESTIC, LOCATION_INTERNATIONAL):
        return value
    return resolve_location(dep_iata, arr_iata, country_by_iata)


def read_flight_number(record: dict) -> str | None:
    """
    Uçuş numarası. Besleme yalnızca tam kodu ("AC72") veriyorsa
    baştaki havayolu harfleri ayrılır.
    """
    number = field(record, "flight_number", "flightNumber", "flightNo")
    if number and not number.strip().isdigit():
        digits = normalize_flight_number(number) or ""
        trimmed = digits.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        return trimmed or digits or None
    return number


def parse_source_a_record(
    record: dict,
    direction: str,
    country_by_iata: dict[str, str],
    aircraft_index: dict[str, str] | None = None,
) -> dict | None:
    """
    Kaynak A kaydını Flight alanlarına eşler ve Kaynak B ile
    zenginleştirir.

    Alan adları iki şemadan da okunur (bkz. `field`), böylece canlı
    beslemeye geçiş bu fonksiyonun dışında hiçbir katmanı etkilemez.

    aircraft_icao önceliği:
      1) Kaynak A'nın kendi alanı (doluysa)
      2) Kaynak B eşleşmesi
      3) None - uydurma değer ÜRETİLMEZ
    """
    dep_iata = (field(record, "dep_iata", "depIata") or "").upper() or None
    arr_iata = (field(record, "arr_iata", "arrIata") or "").upper() or None

    direction = read_direction(record, direction)
    airport_iata = dep_iata if direction == DIRECTION_DEPARTURE else arr_iata
    if not airport_iata:
        return None

    dep_scheduled = parse_utc(
        field(record, "dep_time_utc", "depScheduledUtc")
    )
    airline_iata = (
        field(record, "airline_iata", "airlineIata", "airline") or ""
    ).upper() or None
    flight_number = read_flight_number(record)

    flight_code = field(record, "flight_iata", "flightIata", "flightNo")
    flight_icao = field(record, "flight_icao", "flightIcao")

    own_icao = (
        field(record, "aircraft_icao", "aircraftIcao") or ""
    ).upper() or None
    matched_icao = None
    if own_icao is None and aircraft_index:
        for candidate in (flight_code, flight_icao):
            key = normalize_flight_number(candidate)
            if key and key in aircraft_index:
                matched_icao = aircraft_index[key]
                break

    aircraft_icao = own_icao or matched_icao

    return {
        "flight_key": build_flight_key(airline_iata, flight_number, dep_scheduled),
        "airport_iata": airport_iata,
        "direction": direction,
        "location": read_location(
            record, dep_iata, arr_iata, country_by_iata
        ),
        "airline_iata": airline_iata,
        "flight_number": flight_number,
        "flight_iata": normalize_flight_number(flight_code),
        "aircraft_icao": aircraft_icao,
        "aircraft_match_found": aircraft_icao is not None,
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "dep_scheduled_utc": dep_scheduled,
        "dep_estimated_utc": parse_utc(
            field(record, "dep_estimated_utc", "depEstimatedUtc")
        ),
        "dep_actual_utc": parse_utc(
            field(record, "dep_actual_utc", "depActualUtc")
        ),
        "arr_scheduled_utc": parse_utc(
            field(record, "arr_time_utc", "arrScheduledUtc")
        ),
        "arr_estimated_utc": parse_utc(
            field(record, "arr_estimated_utc", "arrEstimatedUtc")
        ),
        "arr_actual_utc": parse_utc(
            field(record, "arr_actual_utc", "arrActualUtc")
        ),
        "dep_terminal": field(record, "dep_terminal", "depTerminal"),
        "dep_gate": field(record, "dep_gate", "depGate"),
        "arr_terminal": field(record, "arr_terminal", "arrTerminal"),
        "arr_gate": field(record, "arr_gate", "arrGate"),
        "status": (field(record, "status") or "unknown").lower(),
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
    "clean_text",
    "field",
    "load_source_payload",
    "normalize_flight_number",
    "parse_source_a",
    "parse_source_a_record",
    "parse_utc",
    "read_direction",
    "read_flight_number",
    "read_location",
    "resolve_location",
]
