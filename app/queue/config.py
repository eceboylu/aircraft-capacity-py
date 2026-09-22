"""
AŞAMA 1 - Havalimanı bazlı operasyonel config çözümleme.

Kod hiçbir havalimanına özel değer bilmez. Bir havalimanı için
airport_operational_configs tablosunda satır varsa o kullanılır;
yoksa model üzerindeki varsayılanlarla geçici bir görünüm üretilir
ve `is_default=True` işaretlenir - bu işaret AŞAMA 7'de confidence
cezasına dönüşür.

Yeni havalimanı eklemek = tabloya satır eklemek. Kod değişikliği
gerekmez.

ADIM (Airport-Scale Queue Capacity) - ÖNCELİK ZİNCİRİ:
  1) Explicit `AirportOperationalConfig` override (satır varsa,
     alan-bazlı: `passport_departure_server_count`/`passport_arrival_
     server_count` NULL değilse KULLANILIR, NULL ise 2'ye düşülür -
     bir alanın override edilmesi DİĞER scale-derived değerleri
     KAYBETTİRMEZ).
  2) `Airport.scale`'den türetilmiş kaynak eşlemesi
     (`domain/airport_scale.py:SCALE_RESOURCES`).
  3) Scale de bilinmiyorsa (None/tanınmıyor) ESKİ sabit varsayılan
     (4 gişe x 2 görevli = 8 - `_column_defaults()`'tan, TEKRAR
     YAZILMAZ) - "unknown scale" sessizce farklı bir sayıya
     DÜŞMEZ, ESKİ, halihazırda test edilmiş davranışla AYNI kalır.

Bu zincir SADECE bu dosyada çözülür - `engine.py`/`core/scoring.py`
scale'den HABERSİZ kalmaya devam eder, sadece `AirportConfigView`'in
ZATEN çözülmüş `passport_departure_server_count`/`passport_arrival_
server_count`/`domestic_security_lane_count`/`international_security_
lane_count` alanlarını tüketir (hard-code YOK).
"""

from dataclasses import dataclass

from sqlalchemy import select

from .domain.airport_scale import resource_view_for_scale
from .models import Airport, AirportOperationalConfig

# Varsayılanlar burada TEKRAR YAZILMAZ; tek doğruluk kaynağı
# AirportOperationalConfig sütun tanımlarıdır (bkz. models.py).
#
# NOT: `passport_departure_server_count`/`passport_arrival_server_count`
# BİLEREK bu listede DEĞİL - ikisi de nullable, gerçek bir SQL DEFAULT'u
# YOK (`column.default` None) - `_column_defaults()`'un generic
# `column.default.arg` okuması bunlarda patlar. Bu iki alan aşağıda
# `_resolve_passport_server_counts()` ile AYRI, açık mantıkla çözülür.
_CONFIG_FIELDS = (
    "passport_counter_count",
    "passport_staff_count",
    "passport_service_time_minutes",
    "security_lane_count",
    "domestic_security_lane_count",
    "international_security_lane_count",
    "security_service_time_minutes",
    "passport_staff_per_counter",
    "passport_service_rate_per_staff",
    "passport_efficiency_multiplier",
    "arrival_bank_threshold",
)


def _column_defaults() -> dict:
    defaults = {}
    for name in _CONFIG_FIELDS:
        column = AirportOperationalConfig.__table__.columns[name]
        defaults[name] = column.default.arg
    return defaults


def _legacy_unknown_server_count() -> int:
    """
    Eski (Airport-Scale ÖNCESİ) tek-havuz varsayımı: 4 gişe x 2 görevli
    = 8. Scale bilinmiyorsa departure/arrival passport'un İKİSİ de bu
    sayıyı alır - "unknown scale" sessizce YENİ/farklı bir sayıya
    (ör. 0 veya scale'lerden biri) düşmez, ESKİ davranışla birebir aynı
    kalır (bkz. Bölüm 12 - "unknown != silent old default 8" testi,
    burada KASITLI olarak "8" - eski davranışın KENDİSİ, farklı bir
    varsayılan DEĞİL).
    """
    defaults = _column_defaults()
    return round(defaults["passport_counter_count"] * defaults["passport_staff_per_counter"])


@dataclass(kw_only=True)
class AirportConfigView:
    """
    Hesap katmanının gördüğü config. Tablodan gelmiş de olabilir,
    varsayılanlardan türetilmiş de - fark `is_default` ile taşınır.

    `kw_only=True`: tüm mevcut çağıranlar zaten keyword argümanlarıyla
    inşa ediyor (bkz. config.py/testler) - bu, yeni alanların (ör.
    `domestic_security_lane_count`) varsayılan değer alıp dataclass'ın
    "defaultsız alan defaultlu alandan önce olmalı" kısıtına takılmadan
    ORTAYA eklenebilmesini sağlar; mevcut hiçbir çağrı ETKİLENMEZ.
    """

    airport_iata: str
    passport_counter_count: int
    passport_staff_count: int
    passport_service_time_minutes: float
    security_lane_count: int
    # ADIM (Domestic/International Security Lane Ayrımı): mevcut
    # `security_lane_count` ile AYNI varsayılan (8) - bu alanları
    # açıkça vermeyen (ör. eski test yardımcıları) hiçbir çağıran
    # bozulmaz, sadece yeni default değeri alır.
    domestic_security_lane_count: int = 8
    international_security_lane_count: int = 8
    security_service_time_minutes: float
    passport_staff_per_counter: float
    passport_service_rate_per_staff: float
    passport_efficiency_multiplier: float
    arrival_bank_threshold: int
    is_default: bool
    # ADIM (Airport-Scale Queue Capacity) - artık departure/arrival
    # passport AYRI fiziksel havuz (bkz. core/event_queue.py). Eski
    # `passport_counter_count x passport_staff_per_counter` (TEK sayı,
    # `core/scoring.py:passport_effective_server_count()`, geriye
    # dönük uyumluluk için KORUNDU ama production yolu artık BUNLARI
    # kullanır) - varsayılan 8/8, eski TEK-havuz sayısıyla AYNI (mevcut
    # çağıranlar/testler bu iki alanı vermezse davranış DEĞİŞMEZ).
    passport_departure_server_count: int = 8
    passport_arrival_server_count: int = 8
    # ADIM (Dynamic LARGE Passport Staffing) - `True` ise (SADECE scale
    # "large" VE bu havuz için explicit `AirportOperationalConfig`
    # alan-bazlı override YOKSA) `passport_departure_server_count`/
    # `passport_arrival_server_count` yukarıdaki alan, dinamik
    # staffing'in TABANI/DEFAULT'udur - `engine.py` bu bayrak True
    # olduğunda `simulate_fifo_queue_dynamic()` yolunu kullanır.
    # `False` (varsayılan, MEDIUM/SMALL/UNKNOWN VE her explicit override
    # için hep False) ise ESKİ sabit `simulate_fifo_queue()` yolu
    # AYNEN kullanılır - davranış BİREBİR korunur.
    passport_departure_dynamic: bool = False
    passport_arrival_dynamic: bool = False
    # Dynamic staffing'in ramp edebileceği TAVAN - SADECE ilgili
    # `passport_*_dynamic` True ise anlamlıdır; `False` olduğunda
    # `engine.py` bu alanları HİÇ OKUMAZ (statik yol max'tan habersizdir).
    passport_departure_server_count_max: int | None = None
    passport_arrival_server_count_max: int | None = None
    # Traceability - hangi ölçekten türediği (debug/log amaçlı, API'ye
    # SERİLEŞTİRİLMEZ - `api.py` bu sınıfı hiç import etmiyor).
    scale: str | None = None


def _resolve_passport_server_counts(
    row: AirportOperationalConfig | None, resources: dict | None,
) -> tuple[int, int, bool, bool]:
    """
    ÖNCELİK: (1) row'un KENDİ alan-bazlı override'ı (NULL değilse) >
    (2) scale-derived > (3) eski sabit (unknown scale).

    Bir alanın (ör. sadece departure) override edilmesi DİĞERİNİN
    (arrival) scale-derived değerini KAYBETTİRMEZ - ikisi BAĞIMSIZ
    çözülür.

    Döner: (departure, arrival, departure_is_override, arrival_
    is_override) - son ikisi ADIM (Dynamic LARGE Passport Staffing)
    için: bir havuzun dynamic olup olamayacağı SADECE scale=large
    olmasına değil, o havuzun explicit override TAŞIMAMASINA da bağlı
    (bkz. `_build_config_view` - "explicit airport override = fixed
    configuration" kuralı, process bazında AYRI değerlendirilir).
    """
    fallback = _legacy_unknown_server_count()
    scale_dep = resources["departure_passport_servers"] if resources else fallback
    scale_arr = resources["arrival_passport_servers"] if resources else fallback

    departure_is_override = row is not None and row.passport_departure_server_count is not None
    arrival_is_override = row is not None and row.passport_arrival_server_count is not None

    departure = row.passport_departure_server_count if departure_is_override else scale_dep
    arrival = row.passport_arrival_server_count if arrival_is_override else scale_arr

    return departure, arrival, departure_is_override, arrival_is_override


def _resolve_security_lane_counts(
    row: AirportOperationalConfig | None, resources: dict | None,
) -> tuple[int, int]:
    """
    Security lane'ler İÇİN ÖNCELİK satır-seviyesindedir (mevcut, hiç
    değişmeyen davranış): bir `AirportOperationalConfig` satırı VARSA
    onun `domestic_security_lane_count`/`international_security_
    lane_count`'u AYNEN kullanılır (satır zaten "bu havalimanı için
    açıkça ayarlandı" anlamına gelir). Satır YOKSA scale-derived (veya
    scale de yoksa eski sabit 8) kullanılır.
    """
    if row is not None:
        return row.domestic_security_lane_count, row.international_security_lane_count

    fallback = _legacy_unknown_server_count()
    domestic = resources["domestic_security_lanes"] if resources else fallback
    international = resources["international_security_lanes"] if resources else fallback
    return domestic, international


def _build_config_view(
    airport_iata: str, row: AirportOperationalConfig | None, scale: str | None,
) -> AirportConfigView:
    resources = resource_view_for_scale(scale)
    (
        departure_servers, arrival_servers,
        departure_is_override, arrival_is_override,
    ) = _resolve_passport_server_counts(row, resources)
    domestic_lanes, international_lanes = _resolve_security_lane_counts(row, resources)

    # ADIM (Dynamic LARGE Passport Staffing) - SADECE scale'in `_max`
    # taşıyan bir tier olması (LARGE) VE o havuz için explicit override
    # YOKSA dynamic aktif. `resources`'ta `_max` anahtarı yoksa (MEDIUM/
    # SMALL/UNKNOWN - SCALE_RESOURCES'ta bu ölçeklerin sözlüğünde `_max`
    # hiç YOK) dynamic zaten anlamsız kalır (max=None -> engine.py bu
    # bayrağı hiç okumaz ama güvenlik için burada da False'a düşürülür).
    # Kasıtlı olarak `scale == SCALE_LARGE` yerine `departure_max is not
    # None` kontrolü kullanılıyor - bir zamanlar burada sabit `scale ==
    # SCALE_LARGE` kontrolü vardı, artık kaldırılmış olan ayrı bir
    # VERY_LARGE tier'ı eklenince bu, VERY_LARGE havalimanlarını SESSİZCE
    # statik/sabit server sayısına düşürüyordu (peak'te max'a asla ramp
    # ETMİYORDU) - `departure_max is not None` scale-agnostik olduğu
    # için `_max` taşıyan HERHANGİ bir gelecekteki tier için de doğru
    # çalışır, hard-code bir tier adına bağlı DEĞİLDİR.
    departure_max = resources.get("departure_passport_servers_max") if resources else None
    arrival_max = resources.get("arrival_passport_servers_max") if resources else None
    passport_departure_dynamic = not departure_is_override and departure_max is not None
    passport_arrival_dynamic = not arrival_is_override and arrival_max is not None

    if row is not None:
        base_fields = {name: getattr(row, name) for name in _CONFIG_FIELDS}
        is_default = False
    else:
        base_fields = _column_defaults()
        is_default = True

    # Row varsa bile lane alanları scale-derived olabilir (row=None
    # dalında) - `base_fields`'daki ham `domestic_security_lane_count`/
    # `international_security_lane_count` değerlerinin üzerine, yukarıda
    # ÇÖZÜLMÜŞ (resources dahil) değerlerle YAZILIR.
    base_fields["domestic_security_lane_count"] = domestic_lanes
    base_fields["international_security_lane_count"] = international_lanes

    return AirportConfigView(
        airport_iata=airport_iata,
        is_default=is_default,
        passport_departure_dynamic=passport_departure_dynamic,
        passport_arrival_dynamic=passport_arrival_dynamic,
        passport_departure_server_count_max=departure_max if passport_departure_dynamic else None,
        passport_arrival_server_count_max=arrival_max if passport_arrival_dynamic else None,
        passport_departure_server_count=departure_servers,
        passport_arrival_server_count=arrival_servers,
        scale=scale,
        **base_fields,
    )


def default_config(airport_iata: str, scale: str | None = None) -> AirportConfigView:
    """
    Tabloda satırı olmayan havalimanı için varsayım config'i.

    `scale` verilirse (bkz. `get_config`/`get_configs`) passport/
    security kaynakları `Airport.scale`'den türetilir; verilmezse
    (eski çağıranlar - testler, doğrudan çağrılar) ESKİ sabit
    varsayılanlar (8/8/8) KORUNUR - geriye dönük uyumluluk.

    Değerler ÖLÇÜLMÜŞ GERÇEK VERİ DEĞİLDİR; bu yüzden is_default
    True döner ve tahminin confidence'ı düşer.
    """
    return _build_config_view(airport_iata, row=None, scale=scale)


def get_config(session, airport_iata: str) -> AirportConfigView:
    """Havalimanının config'i; satır yoksa (scale-derived veya eski) varsayım config'i."""
    row = session.get(AirportOperationalConfig, airport_iata)
    airport = session.get(Airport, airport_iata)
    scale = airport.scale if airport is not None else None
    return _build_config_view(airport_iata, row=row, scale=scale)


def get_configs(session, airport_codes) -> dict[str, AirportConfigView]:
    """
    Birden çok havalimanının config'i tek (config + scale) sorguda.

    Her havalimanı kendi config'iyle hesaplanır; biri diğerinin
    değerini ASLA kullanmaz.
    """
    codes = list(airport_codes)
    if not codes:
        return {}

    rows = session.execute(
        select(AirportOperationalConfig).where(
            AirportOperationalConfig.airport_iata.in_(codes)
        )
    ).scalars().all()
    rows_by_code = {row.airport_iata: row for row in rows}

    scales = session.execute(
        select(Airport.iata_code, Airport.scale).where(Airport.iata_code.in_(codes))
    ).all()
    scale_by_code = {iata: scale for iata, scale in scales}

    return {
        code: _build_config_view(code, rows_by_code.get(code), scale_by_code.get(code))
        for code in codes
    }
