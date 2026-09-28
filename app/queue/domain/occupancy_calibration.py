from __future__ import annotations

AIRPORT_OCCUPANCY_FACTORS: dict[str, float] = {
    "IST": 0.7089,
    "SAW": 0.7973,
    "AMS": 0.6540,
    "JMK": 0.6835,
    "TZX": 0.95,
    "KOI": 0.0981,
    "ISC": 0.1687,
    "SOG": 0.2757,
    "JTY": 0.5116,
}

SCALE_OCCUPANCY_FACTORS: dict[str, float] = {
    "large": 0.7201,
    "medium": 0.5772,
    "small": 0.3187,
}

GLOBAL_OCCUPANCY_FALLBACK = 1.0


def occupancy_factor_for(airport_iata: str | None, scale: str | None = None) -> tuple[float, str]:
    code = (airport_iata or "").upper()
    if code in AIRPORT_OCCUPANCY_FACTORS:
        return AIRPORT_OCCUPANCY_FACTORS[code], "airport_specific"
    if scale in SCALE_OCCUPANCY_FACTORS:
        return SCALE_OCCUPANCY_FACTORS[scale], "scale_level"
    return GLOBAL_OCCUPANCY_FALLBACK, "global_fallback"
