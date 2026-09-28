
import json
import logging
import re

from sqlalchemy import select

from ..domain.airport_scale import (
    find_cross_scale_conflicts,
    parse_scale_list,
    resolve_airport_scale,
)
from ..models import Airport

logger = logging.getLogger(__name__)

_VALUES_ROW = re.compile(
    r"\((\d+),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*')\)",
    re.DOTALL,
)

_SQL_ESCAPE_MAP = {
    "'": "'",
    '"': '"',
    "\\": "\\",
    "n": "\n",
    "r": "\r",
    "0": "\0",
    "t": "\t",
}
_ESCAPE_PAIR = re.compile(r"\\(.)", re.DOTALL)


def _sql_unescape(value: str) -> str:
    return _ESCAPE_PAIR.sub(
        lambda m: _SQL_ESCAPE_MAP.get(m.group(1), m.group(1)), value
    )


def _unquote(value: str) -> str | None:
    if value is None or value == "NULL":
        return None
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    value = value.replace("''", "'")
    return _sql_unescape(value)


def parse_airports_sql(path: str) -> list[dict]:
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
    rows = parse_airports_sql(path)
    for row in rows:
        session.merge(Airport(**row))
    session.commit()
    return len(rows)


def _parse_and_check_conflicts(
    mega_path: str, large_path: str, medium_path: str, small_path: str,
) -> tuple[dict, dict[str, dict[str, set[str]]], set[str], set[str]]:
    scales = {
        "mega": parse_scale_list(mega_path),
        "large": parse_scale_list(large_path),
        "medium": parse_scale_list(medium_path),
        "small": parse_scale_list(small_path),
    }

    conflicts = find_cross_scale_conflicts(
        scales["mega"], scales["large"], scales["medium"], scales["small"],
    )
    conflicting_iata: set[str] = set()
    for codes in conflicts["iata"].values():
        conflicting_iata |= codes
    conflicting_icao: set[str] = set()
    for codes in conflicts["icao"].values():
        conflicting_icao |= codes

    if conflicting_iata or conflicting_icao:
        logger.warning(
            "airport scale kaynak dosyalarında çakışma bulundu (%d IATA, %d ICAO) - "
            "bu kodlar için scale=None bırakıldı (sessizce SEÇİLMEDİ): iata=%s icao=%s",
            len(conflicting_iata), len(conflicting_icao),
            sorted(conflicting_iata), sorted(conflicting_icao),
        )

    return scales, conflicts, conflicting_iata, conflicting_icao


def import_airport_scales(
    session, mega_path: str, large_path: str, medium_path: str, small_path: str,
) -> dict:
    scales, conflicts, conflicting_iata, conflicting_icao = _parse_and_check_conflicts(
        mega_path, large_path, medium_path, small_path,
    )

    airports = session.execute(select(Airport)).scalars().all()
    matched = 0
    unmatched = 0
    conflicted = 0

    for airport in airports:
        iata = (airport.iata_code or "").upper()
        icao = (airport.icao_code or "").upper()
        if iata in conflicting_iata or (icao and icao in conflicting_icao):
            airport.scale = None
            conflicted += 1
            continue

        scale = resolve_airport_scale(
            airport.iata_code, airport.icao_code,
            scales["mega"], scales["large"], scales["medium"], scales["small"],
        )
        airport.scale = scale
        if scale is not None:
            matched += 1
        else:
            unmatched += 1

    session.commit()

    return {
        "airports_checked": len(airports),
        "matched": matched,
        "unmatched": unmatched,
        "conflicted": conflicted,
        "iata_conflicts": {key: sorted(codes) for key, codes in conflicts["iata"].items()},
        "icao_conflicts": {key: sorted(codes) for key, codes in conflicts["icao"].items()},
    }


def refresh_airport_scales(
    session, mega_path: str, large_path: str, medium_path: str, small_path: str,
) -> dict:
    scales, conflicts, conflicting_iata, conflicting_icao = _parse_and_check_conflicts(
        mega_path, large_path, medium_path, small_path,
    )

    airports = session.execute(select(Airport)).scalars().all()
    updated = 0
    unchanged = 0
    conflicted = 0
    preserved_unmatched = 0

    for airport in airports:
        iata = (airport.iata_code or "").upper()
        icao = (airport.icao_code or "").upper()
        if iata in conflicting_iata or (icao and icao in conflicting_icao):
            if airport.scale is not None:
                airport.scale = None
                updated += 1
            conflicted += 1
            continue

        scale = resolve_airport_scale(
            airport.iata_code, airport.icao_code,
            scales["mega"], scales["large"], scales["medium"], scales["small"],
        )
        if scale is None:
            preserved_unmatched += 1
            continue

        if airport.scale != scale:
            airport.scale = scale
            updated += 1
        else:
            unchanged += 1

    session.commit()

    return {
        "airports_checked": len(airports),
        "updated": updated,
        "unchanged": unchanged,
        "conflicted": conflicted,
        "preserved_unmatched": preserved_unmatched,
        "iata_conflicts": {key: sorted(codes) for key, codes in conflicts["iata"].items()},
        "icao_conflicts": {key: sorted(codes) for key, codes in conflicts["icao"].items()},
    }


def country_lookup(session) -> dict[str, str]:
    rows = session.execute(
        select(Airport.iata_code, Airport.country_code)
        .where(Airport.country_code.isnot(None))
    ).all()
    return {iata: country for iata, country in rows if iata and country}
