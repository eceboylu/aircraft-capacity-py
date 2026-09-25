"""
ADIM (Schengen-Aware Passport Routing) - kullanıcı doğrulanmış bulgu:
Schengen->Schengen bir uçuş (ör. ZRH->FRA) GERÇEKTEN international'dır
(traffic type) ama sınırda pasaport kontrolü YOKTUR. `location`
(domestic/international) DEĞİŞMEDİ - SADECE `requires_passport` adlı
ayrı, ek bir sinyal eklendi.
"""
from datetime import datetime

from app.queue.domain.flows import (
    FLOW_INTERNATIONAL_ARRIVAL,
    FLOW_INTERNATIONAL_DEPARTURE,
    flow_of,
    is_schengen_departure_skipping_passport,
    passport_arrival_flights,
    passport_departure_flights,
    passport_flights,
    security_international_flights,
)
from app.queue.domain.schengen import is_schengen_country, requires_passport_control

from .conftest import make_arrival, make_departure

WHEN = datetime(2026, 3, 10, 9, 0)


# --- app/queue/domain/schengen.py saf birim testleri -----------------

def test_ch_de_is_schengen_to_schengen_no_passport():
    assert is_schengen_country("CH") and is_schengen_country("DE")
    assert requires_passport_control("CH", "DE") is False


def test_ch_fr_is_schengen_to_schengen_no_passport():
    assert requires_passport_control("CH", "FR") is False


def test_ch_gb_requires_passport():
    assert is_schengen_country("GB") is False
    assert requires_passport_control("CH", "GB") is True


def test_ch_tr_requires_passport():
    assert is_schengen_country("TR") is False
    assert requires_passport_control("CH", "TR") is True


def test_us_ch_arrival_requires_passport():
    assert requires_passport_control("US", "CH") is True


def test_ch_ch_domestic_no_passport():
    assert requires_passport_control("CH", "CH") is False


def test_unknown_country_defaults_to_requires_passport():
    """Ülke bilgisi yoksa güvenli taraf: passport GEREKİR (Bölüm 8)."""
    assert requires_passport_control(None, "DE") is True
    assert requires_passport_control("CH", None) is True


# --- flows.py entegrasyonu (Flight.requires_passport zaten hesaplı) --

def test_schengen_departure_remains_international_but_skips_passport():
    """CH->DE (ZRH->FRA benzeri): international KALIR, passport YOK, security VAR."""
    flight = make_departure(when=WHEN, location="international", requires_passport=False)

    assert flow_of(flight) == FLOW_INTERNATIONAL_DEPARTURE  # traffic type DEĞİŞMEDİ
    assert flight in security_international_flights([flight])  # security HÂLÂ var
    assert flight not in passport_flights([flight])
    assert flight not in passport_departure_flights([flight])
    assert is_schengen_departure_skipping_passport(flight) is True


def test_non_schengen_departure_unchanged():
    """CH->GB/CH->TR benzeri: mevcut davranış (passport + security) KORUNUR."""
    flight = make_departure(when=WHEN, location="international", requires_passport=True)

    assert flow_of(flight) == FLOW_INTERNATIONAL_DEPARTURE
    assert flight in security_international_flights([flight])
    assert flight in passport_flights([flight])
    assert flight in passport_departure_flights([flight])
    assert is_schengen_departure_skipping_passport(flight) is False


def test_schengen_arrival_no_arrival_passport_modeled():
    """DE->CH (FRA->ZRH benzeri): international arrival, ama arrival passport YOK."""
    flight = make_arrival(when=WHEN, location="international", requires_passport=False)

    assert flow_of(flight) == FLOW_INTERNATIONAL_ARRIVAL  # traffic type DEĞİŞMEDİ
    assert flight not in passport_flights([flight])
    assert flight not in passport_arrival_flights([flight])


def test_non_schengen_arrival_unchanged():
    """US->CH benzeri: arrival passport KORUNUR."""
    flight = make_arrival(when=WHEN, location="international", requires_passport=True)

    assert flow_of(flight) == FLOW_INTERNATIONAL_ARRIVAL
    assert flight in passport_flights([flight])
    assert flight in passport_arrival_flights([flight])


def test_domestic_flows_unchanged_by_requires_passport_flag():
    """CH->CH: domestic davranış `requires_passport` alanından TAMAMEN bağımsız."""
    domestic_dep = make_departure(when=WHEN, location="domestic", requires_passport=False)
    assert flow_of(domestic_dep) == "domestic_departure"
    assert domestic_dep not in passport_flights([domestic_dep])
    assert domestic_dep not in security_international_flights([domestic_dep])
