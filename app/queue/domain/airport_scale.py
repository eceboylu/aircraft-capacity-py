
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCALE_MEGA = "mega"
SCALE_LARGE = "large"
SCALE_MEDIUM = "medium"
SCALE_SMALL = "small"

SCALES = (SCALE_MEGA, SCALE_LARGE, SCALE_MEDIUM, SCALE_SMALL)

SCALE_RESOURCES: dict[str, dict[str, int]] = {
    SCALE_MEGA: {
        # ADIM (MEGA Dynamic Resource Policy) - source of truth artık
        # bunlar: passport_dep/passport_arr/security_intl'in HEPSİ
        # base'den max'a 5'lik kademelerle (ramp_step) dinamik açılıyor,
        # 5 dakikalık checkpoint ile (bkz. constants.py DEFAULT_DYNAMIC_
        # CONTROL_INTERVAL_MINUTES). Eski değerler (dep base=30/max=45,
        # arr base=35/max=45, security static=20) ARTIK KULLANILMIYOR.
        "departure_passport_servers": 15,
        "departure_passport_servers_max": 40,
        "arrival_passport_servers": 30,
        "arrival_passport_servers_max": 40,
        "domestic_security_lanes": 30,
        "international_security_lanes": 15,
        "international_security_lanes_max": 40,
    },
    SCALE_LARGE: {
        "departure_passport_servers": 10,
        "arrival_passport_servers": 12,
        "domestic_security_lanes": 15,
        "international_security_lanes": 15,
    },
    SCALE_MEDIUM: {
        "departure_passport_servers": 4,
        "arrival_passport_servers": 4,
        "domestic_security_lanes": 3,
        "international_security_lanes": 2,
    },
    SCALE_SMALL: {
        "departure_passport_servers": 2,
        "arrival_passport_servers": 2,
        "domestic_security_lanes": 2,
        "international_security_lanes": 2,
    },
}

_LINE_RE = re.compile(r"^([A-Za-z0-9]{2,4})\s*/\s*([A-Za-z0-9-]{2,4})\s*-\s*(.+)$")
_NO_ICAO_TOKENS = {"", "-", "--", "---"}


def _is_header_placeholder(iata: str, icao: str) -> bool:
    return iata.upper() == "IATA" or icao.upper() == "ICAO"


@dataclass(frozen=True)
class ScaleCodeSet:

    iata_codes: frozenset[str] = field(default_factory=frozenset)
    icao_codes: frozenset[str] = field(default_factory=frozenset)


def parse_scale_list(path: str) -> ScaleCodeSet:
    iata_codes: set[str] = set()
    icao_codes: set[str] = set()

    with open(path, encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            match = _LINE_RE.match(line)
            if not match:
                continue
            iata_raw, icao_raw, _name = match.groups()
            iata = iata_raw.strip().upper()
            icao = icao_raw.strip().upper()
            if _is_header_placeholder(iata, icao):
                continue
            if iata:
                iata_codes.add(iata)
            if icao and icao not in _NO_ICAO_TOKENS:
                icao_codes.add(icao)

    return ScaleCodeSet(iata_codes=frozenset(iata_codes), icao_codes=frozenset(icao_codes))


def find_duplicate_iata(path: str) -> list[str]:
    counts: dict[str, int] = {}
    with open(path, encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            match = _LINE_RE.match(line)
            if not match:
                continue
            iata = match.group(1).strip().upper()
            icao = match.group(2).strip().upper()
            if _is_header_placeholder(iata, icao) or not iata:
                continue
            counts[iata] = counts.get(iata, 0) + 1
    return sorted(code for code, n in counts.items() if n > 1)


def find_cross_scale_conflicts(
    mega: ScaleCodeSet, large: ScaleCodeSet, medium: ScaleCodeSet, small: ScaleCodeSet,
) -> dict[str, dict[str, set[str]]]:
    scales = {
        SCALE_MEGA: mega, SCALE_LARGE: large,
        SCALE_MEDIUM: medium, SCALE_SMALL: small,
    }
    conflicts: dict[str, dict[str, set[str]]] = {"iata": {}, "icao": {}}

    names = list(scales)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            iata_overlap = scales[a].iata_codes & scales[b].iata_codes
            if iata_overlap:
                conflicts["iata"][f"{a}&{b}"] = iata_overlap
            icao_overlap = scales[a].icao_codes & scales[b].icao_codes
            if icao_overlap:
                conflicts["icao"][f"{a}&{b}"] = icao_overlap

    return conflicts


def resolve_airport_scale(
    iata_code: str | None,
    icao_code: str | None,
    mega: ScaleCodeSet,
    large: ScaleCodeSet,
    medium: ScaleCodeSet,
    small: ScaleCodeSet,
) -> str | None:
    iata = (iata_code or "").strip().upper()
    icao = (icao_code or "").strip().upper()
    scales = {
        SCALE_MEGA: mega, SCALE_LARGE: large,
        SCALE_MEDIUM: medium, SCALE_SMALL: small,
    }

    if iata:
        for scale_name in SCALES:
            if iata in scales[scale_name].iata_codes:
                return scale_name

    if icao:
        for scale_name in SCALES:
            if icao in scales[scale_name].icao_codes:
                return scale_name

    return None


def resource_view_for_scale(scale: str | None) -> dict[str, int] | None:
    if scale is None:
        return None
    return SCALE_RESOURCES.get(scale)
