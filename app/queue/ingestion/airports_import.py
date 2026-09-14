"""
flight_airports.sql -> Airport tablosu.

Kaynak bir MySQL dökümüdür; ülke/şehir/timezone bilgisi `customized`
sütunundaki JSON metninin içindedir. Burada o INSERT satırları
ayrıştırılıp SQLite'a taşınır.

Bir kere çalıştırılır; tekrar çalıştırılırsa merge() ile üzerine
yazar, yeni satır açmaz (YASAK 4).
"""

import json
import re

from sqlalchemy import select

from ..models import Airport

# ('IATA', 'ICAO', 'Ad', '{json}') dörtlüsünü yakalar.
_VALUES_ROW = re.compile(
    r"\((\d+),\s*"
    r"(NULL|'(?:[^'\\]|\\.)*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.)*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.)*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.)*')\)",
    re.DOTALL,
)


def _unquote(value: str) -> str | None:
    """SQL string literalini Python metnine çevirir."""
    if value is None or value == "NULL":
        return None
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    return (
        value.replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\\\", "\\")
        .replace("\\n", "\n")
    )


def parse_airports_sql(path: str) -> list[dict]:
    """SQL dökümündeki havalimanı satırlarını sözlüğe çevirir."""
    with open(path, encoding="utf-8") as handle:
        content = handle.read()

    airports: list[dict] = []
    seen: set[str] = set()

    for match in _VALUES_ROW.finditer(content):
        _, iata_raw, icao_raw, name_raw, customized_raw = match.groups()

        iata = (_unquote(iata_raw) or "").strip().upper()
        if not iata or iata in seen:
            continue

        details = {}
        customized = _unquote(customized_raw)
        if customized:
            try:
                details = json.loads(customized)
            except (ValueError, TypeError):
                details = {}

        seen.add(iata)
        airports.append({
            "iata_code": iata,
            "icao_code": (_unquote(icao_raw) or "").strip().upper() or None,
            "airport_name": _unquote(name_raw),
            "city_code": (details.get("city_code") or "").strip().upper() or None,
            "country_code": (details.get("country_code") or "").strip().upper() or None,
            "timezone": details.get("timezone") or None,
        })

    return airports


def import_airports(session, path: str) -> int:
    """Havalimanlarını upsert eder, işlenen satır sayısını döndürür."""
    rows = parse_airports_sql(path)
    for row in rows:
        session.merge(Airport(**row))
    session.commit()
    return len(rows)


def country_lookup(session) -> dict[str, str]:
    """
    {IATA: ülke kodu} sözlüğü.

    location (domestic/international) türetimi bunun üzerinden yapılır;
    her uçuş için ayrı sorgu atılmaz.
    """
    rows = session.execute(
        select(Airport.iata_code, Airport.country_code)
        .where(Airport.country_code.isnot(None))
    ).all()
    return {iata: country for iata, country in rows if iata and country}
