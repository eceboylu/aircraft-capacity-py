"""
MADDE 4 doğrulaması - AirlineFleetSeatConfig'in AircraftCapacityService
.resolve() içinde gerçekten kullanılması + mevcut katmanların regresyon
testleri.

Bellek içi SQLite kullanılır. Buradaki AirlineFleetSeatConfig satırları
AÇIKÇA TEST FIXTURE'ıdır - gerçek bir havayolu filo kaynağını temsil
etmez (repo'da böyle bir kaynak dosyası yok, bu yüzden production
seed.py'ye de eklenmedi).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    AircraftCapacity,
    AircraftCapacityFamily,
    AirlineFleetSeatConfig,
    Base,
    UnknownAircraftType,
)
from app.service import AircraftCapacityService, CapacityResult, DEFAULT_CAPACITY


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def service(session):
    return AircraftCapacityService(session)


def add_fleet(session, icao, airline, seats, fleet_count=None, variant=None):
    """TEST fixture satırı - gerçek filo verisi değildir."""
    session.add(AirlineFleetSeatConfig(
        icao_code=icao, airline_iata=airline, seats=seats,
        fleet_count=fleet_count, variant_name=variant,
    ))
    session.commit()


def add_capacity(session, icao, capacity, source="verified_dataset", confidence="high"):
    session.add(AircraftCapacity(
        icao_code=icao, name=None, category=None,
        capacity=capacity, source=source, confidence=confidence,
    ))
    session.commit()


def add_family(session, prefix, category, capacity, counts=True):
    session.add(AircraftCapacityFamily(
        icao_prefix=prefix, category=category,
        default_capacity=capacity, counts_toward_passenger_total=counts,
    ))
    session.commit()


# --------------------------------------------------------------------
# 1. Exact airline + ICAO match
# --------------------------------------------------------------------

def test_exact_airline_icao_match(session, service):
    add_fleet(session, "A320", "TK", seats=180, fleet_count=10)
    result = service.resolve("A320", "TK")

    assert result.capacity == 180
    assert result.source == "airline_fleet_exact"
    assert result.confidence == "high"
    assert result.counts_toward_passenger_total is True


def test_exact_match_wins_over_aircraft_capacity(session, service):
    """Katman sırası: airline fleet katmanı, genel aircraft_capacity'den ÖNCE gelmeli."""
    add_capacity(session, "A320", capacity=180)
    add_fleet(session, "A320", "TK", seats=186, fleet_count=8)

    result = service.resolve("A320", "TK")

    assert result.capacity == 186
    assert result.source == "airline_fleet_exact"


def test_exact_match_works_without_fleet_count(session, service):
    """Tek varyantta fleet_count olmasa bile exact match geçerli olmalı."""
    add_fleet(session, "B738", "PC", seats=189, fleet_count=None)
    result = service.resolve("B738", "PC")

    assert result.capacity == 189
    assert result.source == "airline_fleet_exact"


# --------------------------------------------------------------------
# 2. Weighted fleet average - kullanıcının verdiği örnekle birebir
# --------------------------------------------------------------------

def test_weighted_fleet_average_matches_worked_example(session, service):
    """
    A320/TK: 180 koltuk x10 + 192 koltuk x5
    (180*10 + 192*5) / (10+5) = (1800+960)/15 = 184
    """
    add_fleet(session, "A320", "TK", seats=180, fleet_count=10, variant="dense")
    add_fleet(session, "A320", "TK", seats=192, fleet_count=5, variant="high_density")

    result = service.resolve("A320", "TK")

    assert result.capacity == 184
    assert result.source == "airline_fleet_weighted_average"
    assert result.confidence == "high"


def test_weighted_average_is_not_a_naive_mean():
    """(180+192)/2 = 186 olurdu - bu YANLIŞ olurdu, ağırlıklı 184 doğru."""
    assert (180 + 192) / 2 == 186
    assert (180 * 10 + 192 * 5) / (10 + 5) == 184


# --------------------------------------------------------------------
# 3. aircraft_capacity fallback
# --------------------------------------------------------------------

def test_falls_back_to_aircraft_capacity_when_no_fleet_row(session, service):
    add_capacity(session, "B77W", capacity=350, source="verified_dataset")
    result = service.resolve("B77W", "TK")

    assert result.capacity == 350
    assert result.source == "verified_dataset"


def test_falls_back_to_aircraft_capacity_when_airline_not_given(session, service):
    """airline_iata verilmezse katman 1-2 hiç denenmez, direkt aircraft_capacity."""
    add_fleet(session, "A320", "TK", seats=180, fleet_count=10)
    add_capacity(session, "A320", capacity=174)

    result = service.resolve("A320", None)

    assert result.capacity == 174
    assert result.source == "verified_dataset"


# --------------------------------------------------------------------
# 4. Family fallback
# --------------------------------------------------------------------

def test_family_fallback_still_works(session, service):
    add_family(session, "A32", "narrowbody", 175)
    result = service.resolve("A320", "XX")

    assert result.capacity == 175
    assert result.source == "family_fallback"
    assert result.counts_toward_passenger_total is True


def test_family_fallback_used_when_fleet_and_capacity_both_missing(session, service):
    add_family(session, "B73", "narrowbody", 170)
    result = service.resolve("B738", "TK")

    assert result.capacity == 170
    assert result.source == "family_fallback"


# --------------------------------------------------------------------
# 5. Unknown aircraft / default
# --------------------------------------------------------------------

def test_unknown_aircraft_returns_default(session, service):
    result = service.resolve("ZZZZ", "TK")

    assert result.capacity == DEFAULT_CAPACITY
    assert result.source == "unknown_default"
    assert result.confidence == "low"


def test_unknown_aircraft_is_flagged(session, service):
    service.resolve("ZZZZ", "TK")
    row = session.get(UnknownAircraftType, "ZZZZ")

    assert row is not None
    assert row.seen_count == 1

    service.resolve("ZZZZ", "TK")
    session.refresh(row)
    assert row.seen_count == 2


def test_empty_icao_returns_default_immediately(session, service):
    """Havayolu da bilinmiyorsa (fleet config yok) mevcut unknown/default davranışı korunur."""
    result = service.resolve("", "TK")
    assert result.capacity == DEFAULT_CAPACITY
    assert result.source == "unknown_default"


def test_empty_icao_with_known_airline_fleet_uses_weighted_average_not_default(session, service):
    """
    Bug 3: aircraft_icao BOŞ ama havayolu (TK) biliniyor ve TK'nin
    filo config'i VARSA, doğrudan 150'ye düşülmemeli - havayolunun
    TÜM filosundaki ağırlıklı ortalama kullanılmalı.
    """
    add_fleet(session, "A320", "TK", seats=180, fleet_count=10)
    add_fleet(session, "A321", "TK", seats=220, fleet_count=5)

    result = service.resolve("", "TK")

    assert result.capacity != DEFAULT_CAPACITY
    assert result.capacity == 193   # (180*10 + 220*5) / 15 = 193.33 -> round(193.33)=193
    assert result.source != "unknown_default"


def test_empty_icao_with_unknown_airline_keeps_default_fallback(session, service):
    """aircraft_icao boş + havayolu TAMAMEN bilinmiyor -> mevcut unknown/default davranışı korunur."""
    result = service.resolve("", "ZZ")
    assert result.capacity == DEFAULT_CAPACITY
    assert result.source == "unknown_default"


def test_empty_icao_with_airline_but_no_fleet_rows_keeps_default_fallback(session, service):
    """aircraft_icao boş + havayolu biliniyor ama o havayolu için hiç fleet config yok -> default korunur."""
    add_fleet(session, "A320", "PC", seats=189, fleet_count=8)   # başka havayolu

    result = service.resolve("", "TK")

    assert result.capacity == DEFAULT_CAPACITY
    assert result.source == "unknown_default"


# --------------------------------------------------------------------
# 6. Airline mismatch
# --------------------------------------------------------------------

def test_airline_mismatch_does_not_use_other_airlines_fleet(session, service):
    """A320/LH filo verisi var ama TK için soruluyor - eşleşmemeli."""
    add_fleet(session, "A320", "LH", seats=168, fleet_count=20)
    add_capacity(session, "A320", capacity=180)

    result = service.resolve("A320", "TK")

    assert result.capacity == 180
    assert result.source == "verified_dataset"


def test_airline_mismatch_falls_through_to_family_if_nothing_else_matches(session, service):
    add_fleet(session, "A320", "LH", seats=168, fleet_count=20)
    add_family(session, "A32", "narrowbody", 175)

    result = service.resolve("A320", "TK")

    assert result.capacity == 175
    assert result.source == "family_fallback"


# --------------------------------------------------------------------
# 7. fleet_count ağırlığının doğru çalışması
# --------------------------------------------------------------------

def test_fleet_count_weighting_skews_toward_larger_fleet(session, service):
    """
    3 varyant, çok farklı ağırlıklar - basit ortalama (210) değil,
    ağırlıklı ortalama (219.4 -> 219, baskın 220'lik filoya yakın)
    olmalı.
    """
    add_fleet(session, "A321", "TK", seats=200, fleet_count=1)
    add_fleet(session, "A321", "TK", seats=220, fleet_count=50)
    add_fleet(session, "A321", "TK", seats=210, fleet_count=1)

    result = service.resolve("A321", "TK")

    naive_mean = round((200 + 220 + 210) / 3)
    expected = round((200 * 1 + 220 * 50 + 210 * 1) / 52)

    assert result.capacity == expected
    assert result.capacity != naive_mean
    assert abs(result.capacity - 220) <= 1


def test_rows_without_fleet_count_are_excluded_from_weighted_average(session, service):
    """
    fleet_count'u olmayan satır ağırlıklı ortalamaya karışmamalı,
    sıfıra bölme hatası da olmamalı.
    """
    add_fleet(session, "A321", "TK", seats=999, fleet_count=None)
    add_fleet(session, "A321", "TK", seats=200, fleet_count=10)
    add_fleet(session, "A321", "TK", seats=220, fleet_count=10)

    result = service.resolve("A321", "TK")

    assert result.capacity == 210
    assert result.source == "airline_fleet_weighted_average"


def test_all_rows_without_fleet_count_falls_back(session, service):
    """
    Birden fazla satır var ama HİÇBİRİNİN fleet_count'u yok -
    ağırlıklı ortalama hesaplanamaz, sıfıra bölme yapılmadan
    bir alt katmana düşülmeli.
    """
    add_fleet(session, "A321", "TK", seats=200, fleet_count=None)
    add_fleet(session, "A321", "TK", seats=220, fleet_count=None)
    add_capacity(session, "A321", capacity=190)

    result = service.resolve("A321", "TK")

    assert result.capacity == 190
    assert result.source == "verified_dataset"


# --------------------------------------------------------------------
# 8. GA/freighter passenger demand dışında kalması
# --------------------------------------------------------------------

def test_general_aviation_excluded_regardless_of_new_layers(session, service):
    add_family(session, "C20", "general_aviation", 0, counts=False)
    result = service.resolve("C208", "TK")

    assert result.counts_toward_passenger_total is False
    assert result.capacity == 0
    assert result.source == "general_aviation_excluded"


def test_general_aviation_not_shadowed_by_absent_fleet_row(session, service):
    """Filo tablosunda hiç satır olmasa da GA katmanı bozulmamalı."""
    add_family(session, "PC12", "general_aviation", 0, counts=False)
    result = service.resolve("PC12", None)

    assert result.counts_toward_passenger_total is False


# --------------------------------------------------------------------
# 9. Mevcut AircraftCapacityService davranışının regresyon testleri
# --------------------------------------------------------------------

def test_regression_aircraft_capacity_layer_unaffected(session, service):
    add_capacity(session, "A388", capacity=500, source="verified_dataset", confidence="high")
    result = service.resolve("a388")   # küçük harf normalize edilmeli

    assert result == CapacityResult("A388", None, 500, "verified_dataset", "high", True)


def test_regression_family_prefix_longest_match_wins(session, service):
    add_family(session, "A3", "widebody", 300)
    add_family(session, "A32", "narrowbody", 175)

    result = service.resolve("A320", None)

    assert result.capacity == 175
    assert result.source == "family_fallback"


def test_regression_unknown_default_unchanged(session, service):
    result = service.resolve("Q999", None)
    assert result.capacity == DEFAULT_CAPACITY
    assert result.confidence == "low"


def test_response_shape_has_all_required_fields(session, service):
    """capacity / source / confidence / aircraft bilgisi hiçbir katmanda bozulmamalı."""
    add_fleet(session, "A320", "TK", seats=180, fleet_count=10)

    for icao, airline in [("A320", "TK"), ("ZZZZ", None), ("", None)]:
        result = service.resolve(icao, airline)
        assert hasattr(result, "icao")
        assert hasattr(result, "airline")
        assert hasattr(result, "capacity")
        assert hasattr(result, "source")
        assert hasattr(result, "confidence")
        assert hasattr(result, "counts_toward_passenger_total")
        assert isinstance(result.capacity, int)
