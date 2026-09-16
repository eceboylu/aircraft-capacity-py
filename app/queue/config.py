"""
AŞAMA 1 - Havalimanı bazlı operasyonel config çözümleme.

Kod hiçbir havalimanına özel değer bilmez. Bir havalimanı için
airport_operational_configs tablosunda satır varsa o kullanılır;
yoksa model üzerindeki varsayılanlarla geçici bir görünüm üretilir
ve `is_default=True` işaretlenir - bu işaret AŞAMA 7'de confidence
cezasına dönüşür.

Yeni havalimanı eklemek = tabloya satır eklemek. Kod değişikliği
gerekmez.
"""

from dataclasses import dataclass

from sqlalchemy import select

from .models import AirportOperationalConfig

# Varsayılanlar burada TEKRAR YAZILMAZ; tek doğruluk kaynağı
# AirportOperationalConfig sütun tanımlarıdır (bkz. models.py).
_CONFIG_FIELDS = (
    "passport_counter_count",
    "passport_staff_count",
    "passport_service_time_minutes",
    "security_lane_count",
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


@dataclass
class AirportConfigView:
    """
    Hesap katmanının gördüğü config. Tablodan gelmiş de olabilir,
    varsayılanlardan türetilmiş de - fark `is_default` ile taşınır.
    """

    airport_iata: str
    passport_counter_count: int
    passport_staff_count: int
    passport_service_time_minutes: float
    security_lane_count: int
    security_service_time_minutes: float
    passport_staff_per_counter: float
    passport_service_rate_per_staff: float
    passport_efficiency_multiplier: float
    arrival_bank_threshold: int
    is_default: bool


def default_config(airport_iata: str) -> AirportConfigView:
    """
    Tabloda satırı olmayan havalimanı için varsayım config'i.

    Değerler ÖLÇÜLMÜŞ GERÇEK VERİ DEĞİLDİR; bu yüzden is_default
    True döner ve tahminin confidence'ı düşer.
    """
    return AirportConfigView(
        airport_iata=airport_iata,
        is_default=True,
        **_column_defaults(),
    )


def get_config(session, airport_iata: str) -> AirportConfigView:
    """Havalimanının config'i; satır yoksa varsayım config'i."""
    row = session.get(AirportOperationalConfig, airport_iata)
    if row is None:
        return default_config(airport_iata)

    return AirportConfigView(
        airport_iata=row.airport_iata,
        is_default=False,
        **{name: getattr(row, name) for name in _CONFIG_FIELDS},
    )


def get_configs(session, airport_codes) -> dict[str, AirportConfigView]:
    """
    Birden çok havalimanının config'i tek sorguda.

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

    configured = {
        row.airport_iata: AirportConfigView(
            airport_iata=row.airport_iata,
            is_default=False,
            **{name: getattr(row, name) for name in _CONFIG_FIELDS},
        )
        for row in rows
    }

    return {code: configured.get(code) or default_config(code) for code in codes}
