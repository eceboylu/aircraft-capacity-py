
import json
import logging
import re
from datetime import datetime, timezone

from ...queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)
from ...queue.domain.retention_time import canonical_operational_time
from ...queue.domain.schengen import requires_passport_control

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^A-Z0-9]")

_MALFORMED_RECORD_EXCEPTIONS = (TypeError, ValueError, KeyError, AttributeError)


def _record_context(record: dict) -> str:
    def _safe(*names):
        for name in names:
            value = record.get(name) if isinstance(record, dict) else None
            if value is not None:
                return value
        return None

    return (
        f"flight_iata={_safe('flight_iata', 'flightIata')!r} "
        f"flight_icao={_safe('flight_icao', 'flightIcao')!r} "
        f"dep_iata={_safe('dep_iata', 'depIata')!r} "
        f"arr_iata={_safe('arr_iata', 'arrIata')!r}"
    )

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)

_NULL_TOKENS = {"", "-", "--", "n/a", "na", "null", "none", "unknown"}

AIRCRAFT_MATCH_MAX_AGE_HOURS = 36.0


def normalize_flight_number(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _NON_ALNUM.sub("", value.upper())
    return cleaned or None


def clean_text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.lower() in _NULL_TOKENS:
        return None
    return stripped


def field(record: dict, *names):
    for name in names:
        value = clean_text(record.get(name))
        if value is not None:
            return value
    return None


def parse_utc(value) -> datetime | None:
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
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict):
        records = payload.get("response", [])
    else:
        records = payload

    return [r for r in records if isinstance(r, dict)]


def _parse_source_b_timestamp(record: dict) -> datetime | None:
    raw = field(record, "updated", "updatedAt")
    if raw is None:
        return None
    try:
        epoch_seconds = float(raw)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def build_aircraft_index(source_b_records: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    skipped_malformed = 0
    for record in source_b_records:
        try:
            icao = (field(record, "aircraft_icao", "aircraftIcao") or "").upper()
            if not icao:
                continue

            time_utc = _parse_source_b_timestamp(record)
            if not time_utc:
                continue

            entry = {
                "icao": icao,
                "time": time_utc,
                "dep_iata": (field(record, "dep_iata", "depIata") or "").upper() or None,
                "arr_iata": (field(record, "arr_iata", "arrIata") or "").upper() or None,
            }

            for name in ("flight_iata", "flightIata", "flight_icao", "flightIcao"):
                key = normalize_flight_number(record.get(name))
                if key:
                    if key not in index:
                        index[key] = []
                    index[key].append(entry)
        except _MALFORMED_RECORD_EXCEPTIONS as exc:
            skipped_malformed += 1
            logger.warning(
                "malformed source-B (live flights) record skipped (%s): %s: %s",
                _record_context(record), type(exc).__name__, exc,
            )
            continue

    if skipped_malformed:
        logger.warning(
            "Source B parse summary: raw=%d indexed_keys=%d skipped_malformed=%d",
            len(source_b_records), len(index), skipped_malformed,
        )
    return index


def build_flight_key(
    airline_iata: str | None,
    flight_number: str | None,
    operational_scheduled_utc: datetime | None,
    airport_iata: str,
    direction: str,
) -> str:
    airline = (airline_iata or "UNK").upper()
    number = flight_number or "UNK"
    date_part = (
        operational_scheduled_utc.date().isoformat()
        if operational_scheduled_utc else "UNKDATE"
    )
    return f"{airline}_{number}_{date_part}_{airport_iata.upper()}_{direction.lower()}"


def resolve_location(
    dep_iata: str | None,
    arr_iata: str | None,
    country_by_iata: dict[str, str],
) -> str:
    dep_country = country_by_iata.get((dep_iata or "").upper())
    arr_country = country_by_iata.get((arr_iata or "").upper())

    if dep_country and arr_country and dep_country == arr_country:
        return LOCATION_DOMESTIC
    return LOCATION_INTERNATIONAL


def read_direction(record: dict, default: str) -> str:
    value = (field(record, "direction") or "").lower()
    if value in (DIRECTION_ARRIVAL, DIRECTION_DEPARTURE):
        return value
    return default


def read_location(
    record: dict, dep_iata: str | None, arr_iata: str | None,
    country_by_iata: dict[str, str],
) -> str:
    value = (field(record, "location") or "").lower()
    if value in (LOCATION_DOMESTIC, LOCATION_INTERNATIONAL):
        return value
    return resolve_location(dep_iata, arr_iata, country_by_iata)


def read_flight_number(record: dict) -> str | None:
    number = field(record, "flight_number", "flightNumber", "flightNo")
    if number and not number.strip().isdigit():
        digits = normalize_flight_number(number) or ""
        trimmed = digits.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        return trimmed or digits or None
    return number


def _physical_flight_key(record: dict) -> tuple:
    return (
        (field(record, "dep_iata", "depIata") or "").upper(),
        (field(record, "arr_iata", "arrIata") or "").upper(),
        field(record, "dep_time_utc", "depScheduledUtc"),
        field(record, "arr_time_utc", "arrScheduledUtc"),
    )


def dedupe_codeshares(records: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for record in records:
        groups.setdefault(_physical_flight_key(record), []).append(record)

    result: list[dict] = []
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        operating = [
            r for r in group
            if not field(r, "cs_flight_iata", "csFlightIata")
        ]
        codeshares = [
            r for r in group
            if field(r, "cs_flight_iata", "csFlightIata")
        ]

        if not codeshares:
            result.extend(group)
        elif operating:
            result.extend(operating)
        else:
            canonical = min(
                codeshares,
                key=lambda r: normalize_flight_number(
                    field(r, "flight_iata", "flightIata")
                ) or "",
            )
            result.append(canonical)

    return result


def codeshare_skip_counts_by_airport(
    records: list[dict], direction: str
) -> dict[str, int]:
    """
    ADIM (Codeshare Audit Attribution) - `dedupe_codeshares()`'ın atladığı
    kayıtları, `queue_routing_summary_audit.codeshare_records_removed`
    (havalimanı başına) doldurabilmek için ilgili havalimanına göre
    sayar. Departure kaydı için ilgili havalimanı `dep_iata`, arrival
    kaydı için `arr_iata`'dır - `parse_source_a_record()`'ın `airport_
    iata` seçimiyle AYNI kural. `dedupe_codeshares()`'ın KENDİSİ
    DEĞİŞTİRİLMEDİ - bu SADECE aynı fonksiyonun sonucunu, atlanan
    kayıtları geri bulmak için tekrar kullanan, salt-okunur bir sayım.
    """
    kept_ids = {id(r) for r in dedupe_codeshares(records)}
    airport_field = "dep_iata" if direction == DIRECTION_DEPARTURE else "arr_iata"
    airport_field_camel = "depIata" if direction == DIRECTION_DEPARTURE else "arrIata"

    counts: dict[str, int] = {}
    for record in records:
        if id(record) in kept_ids:
            continue
        airport = (field(record, airport_field, airport_field_camel) or "").upper()
        if airport:
            counts[airport] = counts.get(airport, 0) + 1
    return counts


def parse_source_a_record(
    record: dict,
    direction: str,
    country_by_iata: dict[str, str],
    aircraft_index: dict[str, str] | None = None,
    min_operational_time: datetime | None = None,
) -> dict | None:
    dep_iata = (field(record, "dep_iata", "depIata") or "").upper() or None
    arr_iata = (field(record, "arr_iata", "arrIata") or "").upper() or None

    direction = read_direction(record, direction)
    airport_iata = dep_iata if direction == DIRECTION_DEPARTURE else arr_iata
    if not airport_iata:
        return None

    dep_scheduled = parse_utc(
        field(record, "dep_time_utc", "depScheduledUtc")
    )
    arr_scheduled = parse_utc(
        field(record, "arr_time_utc", "arrScheduledUtc")
    )
    operational_scheduled = canonical_operational_time(
        direction, dep_scheduled, arr_scheduled
    )

    if (
        min_operational_time is not None
        and operational_scheduled is not None
        and operational_scheduled < min_operational_time
    ):
        return None
    airline_iata = (
        field(record, "airline_iata", "airlineIata", "airline") or ""
    ).upper() or None
    flight_number = read_flight_number(record)

    flight_code = field(record, "flight_iata", "flightIata", "flightNo")
    flight_icao = field(record, "flight_icao", "flightIcao")

    if not (flight_number or flight_code or flight_icao or airline_iata):
        return None

    own_icao = (
        field(record, "aircraft_icao", "aircraftIcao") or ""
    ).upper() or None
    matched_icao = None
    if own_icao is None and aircraft_index and operational_scheduled:
        raw_candidates = []
        for candidate in (flight_code, flight_icao):
            key = normalize_flight_number(candidate)
            if key and key in aircraft_index:
                raw_candidates.extend(aircraft_index[key])

        eligible = []
        for candidate_match in raw_candidates:
            if (
                candidate_match["dep_iata"] is None
                or dep_iata is None
                or candidate_match["dep_iata"] != dep_iata
            ):
                continue
            if (
                candidate_match["arr_iata"] is None
                or arr_iata is None
                or candidate_match["arr_iata"] != arr_iata
            ):
                continue

            age_hours = abs(
                (candidate_match["time"] - operational_scheduled).total_seconds()
            ) / 3600.0
            if age_hours > AIRCRAFT_MATCH_MAX_AGE_HOURS:
                continue

            eligible.append((age_hours, candidate_match["icao"]))

        if eligible:
            best_age = min(age for age, _ in eligible)
            best_icaos = {icao for age, icao in eligible if age == best_age}
            if len(best_icaos) == 1:
                matched_icao = next(iter(best_icaos))

    aircraft_icao = own_icao or matched_icao

    dep_country = country_by_iata.get((dep_iata or "").upper())
    arr_country = country_by_iata.get((arr_iata or "").upper())
    requires_passport = requires_passport_control(dep_country, arr_country)

    return {
        "flight_key": build_flight_key(
            airline_iata, flight_number, operational_scheduled, airport_iata, direction
        ),
        "airport_iata": airport_iata,
        "direction": direction,
        "location": read_location(
            record, dep_iata, arr_iata, country_by_iata
        ),
        "requires_passport": requires_passport,
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
        "arr_scheduled_utc": arr_scheduled,
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
    min_operational_time: datetime | None = None,
) -> list[dict]:
    raw_count = len(records)
    records = dedupe_codeshares(records)
    codeshares_skipped = raw_count - len(records)
    if codeshares_skipped:
        logger.info(
            "Source A codeshare dedup (direction=%s): raw=%d physical=%d skipped=%d",
            direction, raw_count, len(records), codeshares_skipped,
        )

    parsed = []
    skipped_malformed = 0
    for record in records:
        try:
            row = parse_source_a_record(
                record, direction, country_by_iata, aircraft_index,
                min_operational_time=min_operational_time,
            )
        except _MALFORMED_RECORD_EXCEPTIONS as exc:
            skipped_malformed += 1
            logger.warning(
                "malformed source-A record skipped (direction=%s, %s): %s: %s",
                direction, _record_context(record), type(exc).__name__, exc,
            )
            continue
        if row is not None:
            parsed.append(row)

    if skipped_malformed:
        logger.warning(
            "Source A parse summary (direction=%s): raw=%d parsed=%d skipped_malformed=%d",
            direction, len(records), len(parsed), skipped_malformed,
        )
    return parsed


def aircraft_match_rate(rows: list[dict]) -> float:
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
    "codeshare_skip_counts_by_airport",
    "dedupe_codeshares",
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
