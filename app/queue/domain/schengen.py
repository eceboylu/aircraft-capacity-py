
SCHENGEN_COUNTRY_CODES = frozenset({
    "AT", "BE", "BG", "HR", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE",
    "IS", "LI", "NO", "CH",
})


def is_schengen_country(country_code: str | None) -> bool:
    if not country_code:
        return False
    return country_code.strip().upper() in SCHENGEN_COUNTRY_CODES


def requires_passport_control(
    dep_country: str | None, arr_country: str | None
) -> bool:
    if not dep_country or not arr_country:
        return True
    if dep_country == arr_country:
        return False
    if is_schengen_country(dep_country) and is_schengen_country(arr_country):
        return False
    return True
