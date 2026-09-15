"""
AirLabs gerçek API client (`schedules` + `flights` endpoint'leri).

Bu modül SADECE `app/queue/pipeline.py` içinden, `run(source_a=...,
source_b=...)` ile AÇIKÇA geçirilirse devreye girer. Hiçbir varsayılan
davranış otomatik olarak gerçek API'ye bağlanmaz - `file_source_a()`/
`file_source_b()` (pipeline.py) hâlâ `run()`'ın varsayılanıdır.

API key GÜVENLİĞİ:
  - `AIRLABS_API_KEY` ortam değişkeninden okunur, koda hiçbir yerde
    sabit yazılmaz.
  - Hiçbir log/exception mesajı tam URL'yi (api_key dahil) içermez -
    sadece endpoint adı + HTTP durumu loglanır.

Endpoint kararı (bkz. proje planı - AirLabs resmi dokümantasyonundan
doğrulandı, tahmin edilmedi):
  - `schedules` -> BİRİNCİL Kaynak A (departure/arrival tarifesi, TÜM
    uçuşlar - sadece gecikmişler değil).
  - `flights`   -> Kaynak B (aircraft_icao enrichment, canlı ADS-B feed).
  - `delays`    -> KULLANILMIYOR: sadece 30+ dk gecikmiş uçuşları
    döndürür, tek başına flight_count'u sistematik eksik gösterir.
  - `historical` -> KULLANILMIYOR: tek-uçuş bazlı sorgu (flight_iata/
    flight_icao zorunlu), toplu havalimanı taraması yapılamaz. AirLabs
    dokümantasyonunda "diverted" durumunun TEK dokümante edildiği yer
    burasıdır - bu modülün ürettiği kayıtlarda `status="diverted"`
    GERÇEKÇİ ŞEKİLDE OLUŞMAYABİLİR; bu açık bir kısıttır, uydurulmaz.
"""

from __future__ import annotations

import json
import logging
import os
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
# Sınırlı, üstel backoff - sadece retriable hatalarda (429, 5xx, bağlantı).
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# ADIM 5D - sayfalama güvenliği.
#
# ÖNEMLİ: Aşağıdaki iki sabit "kaç kayıt alınabilir" sınırı DEĞİLDİR.
# `request.has_more=True` olduğu ve API GERÇEKTEN ilerlediği sürece
# `_request()` VERİ KAYBI OLMADAN sayfalamaya devam eder - tek başına
# "çok sayfa var" hiçbir zaman erken kesme sebebi değildir.
#
# Bu sabitler SADECE API'nin BOZUK davrandığı (offset'i hiç dikkate
# almıyor, aynı sayfayı tekrar tekrar dönüyor, has_more'u hiçbir zaman
# False yapmıyor) durumlara karşı SON ÇARE devre kesicilerdir:
#
#   _STUCK_PAGE_REPEAT_LIMIT      : arka arkaya AYNI İÇERİKLİ sayfa
#                                    kaç kez gelirse "API offset'e
#                                    uymuyor" kabul edilip durulacağı.
#   ABSOLUTE_SAFETY_PAGE_LIMIT    : has_more hiç düşmese ve her sayfa
#                                    GERÇEKTEN farklı/yeni içerik
#                                    getirse bile (yani "bozuk"
#                                    sayılmayan, sadece anormal derecede
#                                    uzun bir akış), sonsuz döngüye
#                                    girmemek için son bir tavan.
#                                    Gerçekçi hiçbir AirLabs kullanım
#                                    senaryosunda (tek havalimanı, ~10
#                                    saatlik schedules penceresi)
#                                    ulaşılması BEKLENMEZ - bu yüzden
#                                    eski MAX_PAGES=50 değerinin aksine
#                                    (ADIM 5C'de 3.000 kayıtta 500
#                                    kaydın SESSİZCE kaybolduğu
#                                    kanıtlanmıştı) çok yüksek tutulur
#                                    ve tetiklendiğinde SESSİZ DEĞİL,
#                                    açık bir logger.error() üretir.
_STUCK_PAGE_REPEAT_LIMIT = 2
ABSOLUTE_SAFETY_PAGE_LIMIT = 5000


class AirLabsError(OSError):
    """
    Bilinçli olarak OSError alt sınıfı: `pipeline.py`'nin MEVCUT
    `except (OSError, ValueError)` bloğu (Kaynak B için zaten vardı)
    bu hatayı da yakalar - pipeline.py'nin hata yakalama mantığı
    DEĞİŞTİRİLMEDEN yeni client'a uyum sağlar.
    """


class AirLabsAuthError(AirLabsError):
    """401/403 - kimlik hatası, retry edilmez."""


def _api_key() -> str:
    """
    `AIRLABS_API_KEY` ortam değişkenini okur. Modül İMPORT edilirken
    DEĞİL, ilk gerçek istek anında çağrılır - böylece key olmadan da
    modül sorunsuz import edilebilir (mevcut test suite bundan
    etkilenmez).
    """
    key = os.environ.get("AIRLABS_API_KEY")
    if not key:
        raise RuntimeError(
            "AIRLABS_API_KEY ortam değişkeni tanımlı değil - "
            "AirLabs client kullanılamaz."
        )
    return key


def _retry_wait(exc: urllib.error.HTTPError, attempt: int) -> float:
    """429 için Retry-After header'ı varsa onu, yoksa üstel backoff'u kullanır."""
    if exc.code == 429 and exc.headers is not None:
        retry_after = exc.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return _BACKOFF_SECONDS[min(attempt - 1, len(_BACKOFF_SECONDS) - 1)]


def _request_page(endpoint: str, params: dict) -> tuple[list[dict], bool]:
    """
    TEK bir sayfa isteği (retry/hata mantığı dahil).

    `endpoint`: "schedules" | "flights". `(records, has_more)` döner.
    `has_more`, AirLabs'ın gerçek response zarfındaki
    `request.has_more` alanından okunur (fixture'larda doğrulandı -
    `response` içinde DEĞİL, `request` içindedir). Log/exception
    mesajlarında `api_key` veya tam query string ASLA yer almaz -
    sadece `endpoint` adı ve HTTP durumu.
    """
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
        except json.JSONDecodeError:
            logger.error(
                "AirLabs geçersiz JSON döndürdü (endpoint=%s) - retry edilmiyor",
                endpoint,
            )
            raise

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
    """
    Bir sayfanın içerik parmak izi - "API offset'i yok sayıp AYNI
    sayfayı tekrar tekrar mı dönüyor" tespiti için. Alan şemasından
    bağımsız çalışması için kaydın TAMAMI kullanılır (belirli bir
    alana - ör. flight_iata - bağımlı değildir).
    """
    try:
        return json.dumps(records, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(records)


def _request(endpoint: str, params: dict, max_pages: int | None = None) -> list[dict]:
    """
    Sayfalama sarmalayıcısı - `request.has_more` True olduğu ve API
    GERÇEKTEN ilerlediği sürece `offset`'i bir önceki sayfada
    GERÇEKTEN dönen kayıt sayısı kadar ilerleterek devam eder; sadece
    "çok sayfa var" diye kayıt KESMEZ (bkz. ADIM 5D - eski sabit
    MAX_PAGES=50 tavanı, 3.000 kayıtlık gerçekçi bir senaryoda 500
    kaydı SESSİZCE kaybediyordu).

    `limit` çağıran taraftan `params` içinde verilmediyse hiç
    eklenmez - AirLabs kendi varsayılanını (plana göre değişir)
    kullanır; biz sadece dönen sayfanın büyüklüğüne göre offset'i
    ilerletiriz, sabit bir sayfa boyutu VARSAYMAYIZ.

    İki BAĞIMSIZ, veri kaybını SESSİZCE kabul etmeyen güvenlik freni var:

    1) Aynı sayfa arka arkaya `_STUCK_PAGE_REPEAT_LIMIT` kez tekrar
       gelirse (API offset'i yok sayıyor gibi görünüyorsa) -> açık bir
       `logger.error()` ile durur.
    2) Sayfa sayısı `max_pages` (verilmezse `ABSOLUTE_SAFETY_PAGE_LIMIT`)
       tavanına ulaşırsa (her sayfa GERÇEKTEN farklı olsa bile, has_more
       hiç düşmüyorsa) -> yine açık bir `logger.error()` ile durur; bu
       gerçekçi hiçbir AirLabs kullanımında beklenmeyen, sadece son
       çare bir devre kesicidir.

    Boş bir sayfa (`records == []`) - has_more True olsa bile - her
    zaman NORMAL bir bitiş sayılır (ilerlenecek veri yok).
    """
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
            # Boş sayfa: ilerlenecek veri yok - has_more ne derse desin
            # güvenli bir bitiş (offset'i sonsuza kadar hiç ilerletmeyen
            # bir döngüye asla girilmez).
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
    """
    Tek havalimanı için `schedules` çağrısı (gerekiyorsa TÜM
    sayfaları toplar). Departure'da `dep_iata`, arrival'da `arr_iata`
    filtresi kullanılır (AirLabs "en az bir filtre zorunlu" kısıtı -
    tüm dünya tek çağrıda gelmez).
    """
    param_name = "dep_iata" if direction == DIRECTION_DEPARTURE else "arr_iata"
    return _request("schedules", {param_name: airport_iata})


def fetch_live_flights() -> list[dict]:
    """Kaynak B - canlı ADS-B feed'i (`flights` endpoint'i)."""
    return _request("flights", {})


def build_source_a(airports: list[str]) -> Callable[[str], list[dict]]:
    """
    `pipeline.py`'nin `file_source_a()` (dosya okuyucu varsayılan) ile
    AYNI desende bir factory: `(direction) -> list[dict]` imzalı bir
    callable döner, `run(source_a=build_source_a([...]))` ile geçirilir.

    Verilen her havalimanı için ayrı `schedules` çağrısı yapıp
    birleştirir - mevcut `source_a(direction)` arayüzü DEĞİŞMEDEN
    gerçek API'ye bağlanmış olur.

    ADIM 5G-1 - havalimanları birbirinden İZOLE edilir: bir
    havalimanının `fetch_schedules()` çağrısı (retry'ler tükenip)
    `AirLabsError` (401/403 dahil - `AirLabsAuthError` bunun alt
    sınıfı) fırlatırsa, SADECE o havalimanı atlanır - önceki
    havalimanlarından zaten toplanmış kayıtlar KORUNUR, döngü sonraki
    havalimanına DEVAM eder. Tek bir havalimanının geçici hatası
    yüzünden bu yöndeki (departure/arrival) TÜM havalimanlarının
    verisi sıfırlanmaz (bkz. ADIM 5F audit - eski davranış tam olarak
    buydu).

    `AirLabsError` DIŞINDAKİ hatalar (ör. `AIRLABS_API_KEY` hiç
    tanımlı değilse `_api_key()`'in fırlattığı `RuntimeError`) burada
    YAKALANMAZ - bu havalimanına özel değil, sistemin genel bir
    konfigürasyon hatasıdır ve her havalimanında AYNI şekilde
    başarısız olacağı için erken ve açık biçimde yukarı taşınmalıdır.

    Tüm havalimanları başarısız olsa bile bu fonksiyon exception
    FIRLATMAZ - boş liste döner, her başarısızlık kendi ERROR logunu
    üretir (sessiz kayıp YOK, ama process çökmez).
    """
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
    """`file_source_b()` ile aynı desende, gerçek `flights` çağrısına sarar."""
    def provide() -> list[dict]:
        return fetch_live_flights()
    return provide
