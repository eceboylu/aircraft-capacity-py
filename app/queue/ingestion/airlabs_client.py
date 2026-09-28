
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from ..constants import DIRECTION_DEPARTURE

logger = logging.getLogger(__name__)

BASE_URL = "https://airlabs.co/api/v9"
DEFAULT_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

_STUCK_PAGE_REPEAT_LIMIT = 2
ABSOLUTE_SAFETY_PAGE_LIMIT = 5000


class AirLabsError(OSError):
    pass


class AirLabsAuthError(AirLabsError):
    pass


def _api_key() -> str:
    key = os.environ.get("AIRLABS_API_KEY")
    if not key:
        raise RuntimeError(
            "AIRLABS_API_KEY ortam değişkeni tanımlı değil - "
            "AirLabs client kullanılamaz."
        )
    return key


class TrackedAirportsConfigError(RuntimeError):
    pass


_IATA_CODE_RE = re.compile(r"^[A-Z]{3}$")


def tracked_airports_from_env(env_var: str = "AIRLABS_TRACKED_AIRPORTS") -> list[str]:
    raw = os.environ.get(env_var, "")
    seen: dict[str, None] = {}
    invalid: list[str] = []

    for piece in raw.split(","):
        code = piece.strip().upper()
        if not code:
            continue
        if not _IATA_CODE_RE.match(code):
            invalid.append(piece.strip() or "<empty>")
            continue
        seen.setdefault(code, None)

    if invalid:
        raise TrackedAirportsConfigError(
            f"{env_var} içinde geçersiz IATA kodu/kodları var: "
            f"{invalid!r} - her kod tam olarak 3 alfabetik karakter "
            "olmalı (ör. 'IST'). Konfigürasyon TAMAMEN reddedildi, "
            "kısmi/geçerli-olanları-kullan davranışı YOK."
        )

    airports = list(seen.keys())
    if not airports:
        raise TrackedAirportsConfigError(
            f"{env_var} boş veya tanımlı değil - production'da izlenecek "
            "en az bir havalimanı (IATA kodu) AÇIKÇA belirtilmelidir."
        )
    return airports


def _retry_wait(exc: urllib.error.HTTPError, attempt: int) -> float:
    if exc.code == 429 and exc.headers is not None:
        retry_after = exc.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]


def _request_page(endpoint: str, params: dict) -> tuple[list[dict], bool]:
    query = dict(params)
    query["api_key"] = _api_key()
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(query)}"

    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(
                request, timeout=DEFAULT_TIMEOUT_SECONDS
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                logger.error(
                    "AirLabs kimlik hatası (endpoint=%s, status=%s)",
                    endpoint, exc.code,
                )
                raise AirLabsAuthError(
                    f"AirLabs kimlik hatası: {endpoint} -> HTTP {exc.code}"
                ) from exc

            if exc.code == 429 or 500 <= exc.code < 600:
                last_error = exc
                wait = _retry_wait(exc, attempt)
                logger.warning(
                    "AirLabs geçici hata (endpoint=%s, status=%s), "
                    "%.1fs sonra tekrar denenecek (deneme %d/%d)",
                    endpoint, exc.code, wait, attempt, MAX_ATTEMPTS,
                )
                if attempt < MAX_ATTEMPTS:
                    time.sleep(wait)
                continue

            logger.error(
                "AirLabs HTTP hatası (endpoint=%s, status=%s)",
                endpoint, exc.code,
            )
            raise AirLabsError(
                f"AirLabs HTTP hatası: {endpoint} -> {exc.code}"
            ) from exc

        except urllib.error.URLError as exc:
            last_error = exc
            logger.warning(
                "AirLabs bağlantı hatası (endpoint=%s): %s, "
                "tekrar denenecek (deneme %d/%d)",
                endpoint, type(exc.reason).__name__, attempt, MAX_ATTEMPTS,
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(_BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)])
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "AirLabs geçersiz JSON döndürdü (endpoint=%s) - retry edilmiyor",
                endpoint,
            )
            raise AirLabsError(
                f"AirLabs geçersiz JSON döndürdü: {endpoint}"
            ) from exc

        if not isinstance(payload, dict):
            return [], False
        records = [
            r for r in payload.get("response", []) if isinstance(r, dict)
        ]
        has_more = bool(payload.get("request", {}).get("has_more"))
        return records, has_more

    logger.error(
        "AirLabs isteği %d denemeden sonra başarısız (endpoint=%s)",
        MAX_ATTEMPTS, endpoint,
    )
    raise AirLabsError(
        f"AirLabs isteği {MAX_ATTEMPTS} denemeden sonra başarısız: {endpoint}"
    ) from last_error


def _page_fingerprint(records: list[dict]) -> str:
    try:
        return json.dumps(records, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(records)


def _request(endpoint: str, params: dict, max_pages: int | None = None) -> list[dict]:
    limit = max_pages if max_pages is not None else ABSOLUTE_SAFETY_PAGE_LIMIT

    all_records: list[dict] = []
    offset = 0
    previous_fingerprint: str | None = None

    for _ in range(limit):
        page_params = dict(params)
        if offset:
            page_params["offset"] = offset

        records, has_more = _request_page(endpoint, page_params)

        if not records:
            return all_records

        fingerprint = _page_fingerprint(records)
        if fingerprint == previous_fingerprint:
            logger.error(
                "AirLabs sayfalama DURDU: aynı sayfa içeriği arka arkaya "
                "tekrar geldi (endpoint=%s, offset=%d) - API offset "
                "parametresini dikkate almıyor gibi görünüyor. Bu VERİ "
                "KAYBI riski taşıyan bozuk bir API davranışıdır; bu turda "
                "toplanan %d kayıtla duraklatıldı.",
                endpoint, offset, len(all_records),
            )
            return all_records
        previous_fingerprint = fingerprint

        all_records.extend(records)

        if not has_more:
            return all_records

        offset += len(records)

    logger.error(
        "AirLabs sayfalama MUTLAK güvenlik sınırına (%d sayfa) ulaştı "
        "(endpoint=%s) - has_more hâlâ True ve her sayfa GERÇEKTEN farklı "
        "görünüyor; bu gerçekçi bir AirLabs kullanım senaryosunda "
        "BEKLENMEZ, API'nin has_more'u hiç False yapmadığından "
        "ŞÜPHELENİLMELİ. Bu turda toplanan %d kayıtla duraklatıldı - "
        "bu bir 'normal ama çok sayfalı' kesme DEĞİLDİR.",
        limit, endpoint, len(all_records),
    )
    return all_records


def fetch_schedules(direction: str, airport_iata: str) -> list[dict]:
    param_name = "dep_iata" if direction == DIRECTION_DEPARTURE else "arr_iata"
    return _request("schedules", {param_name: airport_iata})


def fetch_live_flights() -> list[dict]:
    return _request("flights", {})


def build_source_a(airports: list[str]) -> Callable[[str], list[dict]]:
    def provide(direction: str) -> list[dict]:
        records: list[dict] = []
        for airport_iata in airports:
            try:
                records.extend(fetch_schedules(direction, airport_iata))
            except AirLabsError:
                logger.error(
                    "AirLabs schedules fetch failed airport=%s direction=%s "
                    "- bu havalimanı bu turda atlanıyor, diğer havalimanları "
                    "etkilenmeden devam ediyor",
                    airport_iata, direction,
                )
                continue
        return records
    return provide


def build_source_b() -> Callable[[], list[dict]]:
    def provide() -> list[dict]:
        return fetch_live_flights()
    return provide
