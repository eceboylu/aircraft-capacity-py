"""
Adım 4 doğrulaması - 9 tespit fonksiyonu.

Özellikle YASAK 2: reasons listesine sadece bu 9 fonksiyondan
üretilmiş maddeler girebilir, hiçbiri tetiklenmezse tek bir
"Normal operasyonel yoğunluk" maddesi yazılır.
"""

import pytest

from app.queue.domain.demand import DemandCalculator
from app.queue.reasons.detector import (
    DetectedReason,
    detect_aircraft_changes,
    detect_arrival_bank,
    detect_cancellation,
    detect_clustering,
    detect_delay_compression,
    detect_diversions,
    detect_intl_share,
    detect_reasons,
    detect_utilization,
    detect_widebody,
)
from tests.factories import MockCapacityResolver, arrival, departure


ALLOWED_CODES = {
    "clustering", "widebody", "delay_compression", "single_delay",
    "utilization", "intl_share", "arrival_bank", "cancellation",
    "aircraft_change", "diversion", "normal",
}


class MockConfig:
    def __init__(self, arrival_bank_threshold=5):
        self.arrival_bank_threshold = arrival_bank_threshold


@pytest.fixture
def calc():
    return DemandCalculator(MockCapacityResolver())


def run_all(process, window_flights, calc, **kwargs):
    """detect_reasons'ı varsayılan argümanlarla çağırmak için kısayol."""
    params = dict(
        airport_iata="AAA",
        process=process,
        window_flights=window_flights,
        all_period_flights=kwargs.pop("all_period_flights", window_flights),
        historical_baseline=kwargs.pop("historical_baseline", None),
        rho=kwargs.pop("rho", None),
        config=kwargs.pop("config", MockConfig()),
        seat_capacity_fn=calc.seat_capacity,
        demand_fn=calc.passenger_demand,
        window_label=kwargs.pop("window_label", "08:00-08:15"),
    )
    params.update(kwargs)
    return detect_reasons(**params)


# --------------------------------------------------------------------
# Neden 1 - Clustering
# --------------------------------------------------------------------

def test_clustering_triggers_above_threshold():
    flights = [departure(8, i) for i in range(15)]     # baseline 10 -> 1.5
    reason = detect_clustering(flights, 10, "08:00-08:15")
    assert reason is not None
    assert reason.code == "clustering"
    assert "1.5 katı" in reason.message
    assert reason.metric_value == pytest.approx(1.5)


def test_clustering_not_triggered_at_threshold():
    """1.4 tam sınır - eşiği GEÇMELİ, eşitlik tetiklemez."""
    flights = [departure(8, i) for i in range(14)]
    assert detect_clustering(flights, 10, "w") is None


def test_clustering_skipped_without_baseline():
    """Sahte baseline üretilmez, neden atlanır."""
    flights = [departure(8, i) for i in range(50)]
    assert detect_clustering(flights, None, "w") is None
    assert detect_clustering(flights, 0, "w") is None


# --------------------------------------------------------------------
# Neden 2 - Wide-body
# --------------------------------------------------------------------

def test_widebody_triggers_on_count(calc):
    flights = [
        departure(8, 0, aircraft="B77W", key="A"),
        departure(8, 5, aircraft="A388", key="B"),
        departure(8, 10, aircraft="A320", key="C"),
    ]
    reason = detect_widebody(flights, calc.seat_capacity, calc.passenger_demand)
    assert reason is not None
    assert reason.code == "widebody"
    assert reason.metric_value == 2.0
    assert "2 geniş gövde" in reason.message


def test_widebody_triggers_on_share_with_single_aircraft(calc):
    """1 widebody / 2 uçuş = 0.5 > 0.3 -> sayı eşiği geçilmese de tetiklenir."""
    flights = [
        departure(8, 0, aircraft="B77W", key="A"),
        departure(8, 5, aircraft="A320", key="B"),
    ]
    reason = detect_widebody(flights, calc.seat_capacity, calc.passenger_demand)
    assert reason is not None
    assert reason.metric_value == 1.0


def test_widebody_not_triggered_for_narrowbody_only(calc):
    flights = [departure(8, i, aircraft="A320", key=f"K{i}") for i in range(5)]
    assert detect_widebody(flights, calc.seat_capacity, calc.passenger_demand) is None


def test_widebody_boundary_250_counts(calc):
    """Eşik 250+ dahil."""
    resolver = MockCapacityResolver(capacities={"TEST": 250})
    c = DemandCalculator(resolver)
    flights = [
        departure(8, 0, aircraft="TEST", key="A"),
        departure(8, 5, aircraft="TEST", key="B"),
    ]
    assert detect_widebody(flights, c.seat_capacity, c.passenger_demand) is not None


def test_widebody_ignores_general_aviation(calc):
    """Genel havacılık kapasitesi 0 döner, widebody sayılmaz."""
    c = DemandCalculator(MockCapacityResolver(excluded={"C208"}))
    flights = [departure(8, i, aircraft="C208", key=f"K{i}") for i in range(5)]
    assert detect_widebody(flights, c.seat_capacity, c.passenger_demand) is None


# --------------------------------------------------------------------
# Neden 3 - Delay compression
# --------------------------------------------------------------------

def test_delay_compression_triggers_with_two_delayed():
    flights = [
        departure(8, 0, delay=30, key="A"),
        departure(8, 5, delay=50, key="B"),
    ]
    reason = detect_delay_compression(flights)
    assert reason.code == "delay_compression"
    assert reason.metric_value == 2.0
    assert "ort. 40 dk" in reason.message


def test_single_delay_is_info_only():
    """Senaryo 4: tek gecikmiş uçuş riski artırmaz, bilgi notu olur."""
    flights = [departure(8, 0, delay=25, key="A"), departure(8, 5, key="B")]
    reason = detect_delay_compression(flights)
    assert reason.code == "single_delay"
    assert reason.severity == "info"
    assert reason.message == "1 uçuş 25 dakika gecikmeli"


def test_delay_under_10_minutes_ignored():
    flights = [departure(8, 0, delay=10, key="A"), departure(8, 5, delay=5, key="B")]
    assert detect_delay_compression(flights) is None


def test_no_delay_returns_none():
    assert detect_delay_compression([departure(8, 0)]) is None


# --------------------------------------------------------------------
# Neden 4 - Utilization (sadece passport)
# --------------------------------------------------------------------

def test_utilization_triggers_for_passport():
    reason = detect_utilization("passport", 0.95)
    assert reason.code == "utilization"
    assert "0.95" in reason.message
    assert "95%" in reason.message


def test_utilization_not_triggered_at_threshold():
    assert detect_utilization("passport", 0.9) is None


def test_utilization_never_for_security():
    """Security'de kapasite metriği YOKTUR."""
    assert detect_utilization("security", 0.99) is None
    assert detect_utilization("security", None) is None


# --------------------------------------------------------------------
# Neden 5 - International share (sadece passport)
# --------------------------------------------------------------------

def test_intl_share_triggers_for_passport():
    flights = [
        departure(8, 0, location="international", key="A"),
        departure(8, 5, location="international", key="B"),
        departure(8, 10, location="domestic", key="C"),
    ]
    reason = detect_intl_share("passport", flights)
    assert reason.code == "intl_share"
    assert "67%" in reason.message


def test_intl_share_not_triggered_at_threshold():
    flights = (
        [departure(8, i, location="international", key=f"I{i}") for i in range(3)]
        + [departure(8, i, location="domestic", key=f"D{i}") for i in range(2)]
    )
    assert detect_intl_share("passport", flights) is None   # tam 0.6


def test_intl_share_never_for_security():
    flights = [departure(8, i, location="international", key=f"K{i}") for i in range(5)]
    assert detect_intl_share("security", flights) is None


# --------------------------------------------------------------------
# Neden 6 - Arrival bank (sadece passport)
# --------------------------------------------------------------------

def test_arrival_bank_triggers_at_threshold():
    flights = [arrival(8, i, key=f"A{i}") for i in range(5)]
    reason = detect_arrival_bank("passport", flights, threshold=5)
    assert reason.code == "arrival_bank"
    assert reason.metric_value == 5.0
    assert "5 uçuş aynı 15 dk pencerede iniyor" == reason.message


def test_arrival_bank_respects_airport_override():
    """YASAK 5: eşik havalimanı bazlı değişebilmeli."""
    flights = [arrival(8, i, key=f"A{i}") for i in range(5)]
    assert detect_arrival_bank("passport", flights, threshold=8) is None
    assert detect_arrival_bank("passport", flights, threshold=3) is not None


def test_arrival_bank_ignores_departures():
    flights = [departure(8, i, key=f"D{i}") for i in range(10)]
    assert detect_arrival_bank("passport", flights, threshold=5) is None


def test_arrival_bank_never_for_security():
    flights = [arrival(8, i, key=f"A{i}") for i in range(10)]
    assert detect_arrival_bank("security", flights, threshold=5) is None


# --------------------------------------------------------------------
# Neden 7 / 9 - Cancellation ve Diversion
# --------------------------------------------------------------------

def test_cancellation_detected():
    flights = [
        departure(8, 0, key="A"),
        departure(8, 5, status="cancelled", key="B"),
        departure(8, 10, status="cancelled", key="C"),
    ]
    reason = detect_cancellation(flights)
    assert reason.code == "cancellation"
    assert reason.severity == "info"
    assert reason.metric_value == 2.0


def test_no_cancellation_returns_none():
    assert detect_cancellation([departure(8, 0)]) is None


def test_diversion_detected_per_flight():
    flights = [
        departure(8, 0, key="A"),
        departure(8, 5, status="diverted", key="DIV1"),
        departure(8, 10, status="diverted", key="DIV2"),
    ]
    found = detect_diversions(flights)
    assert len(found) == 2
    assert all(r.code == "diversion" for r in found)
    assert "DIV1 yönlendirildi, talepten çıkarıldı" == found[0].message


# --------------------------------------------------------------------
# Neden 8 - Aircraft change
# --------------------------------------------------------------------

def capacity_lookup(icao):
    return MockCapacityResolver().resolve(icao).capacity


def test_aircraft_change_positive_delta():
    changes = {"XX_100_2026-09-14": ("A320", "B77W")}   # 180 -> 350
    found = detect_aircraft_changes(changes, capacity_lookup)
    assert len(found) == 1
    assert found[0].code == "aircraft_change"
    assert found[0].metric_value == 170.0
    assert found[0].severity == "warning"
    assert "kapasite +170 yolcu" in found[0].message


def test_aircraft_change_negative_delta_is_info():
    changes = {"XX_100_2026-09-14": ("B77W", "A320")}   # 350 -> 180
    found = detect_aircraft_changes(changes, capacity_lookup)
    assert found[0].metric_value == -170.0
    assert found[0].severity == "info"
    assert "kapasite -170 yolcu" in found[0].message


def test_aircraft_change_zero_delta_not_triggered():
    """Aynı kapasiteye sahip farklı tip -> delta 0, tetiklenmez."""
    changes = {"K": ("EQ1", "EQ2")}
    lookup = MockCapacityResolver(capacities={"EQ1": 200, "EQ2": 200}).resolve
    assert detect_aircraft_changes(changes, lambda i: lookup(i).capacity) == []


def test_aircraft_change_ignores_missing_sides():
    assert detect_aircraft_changes({"K": (None, "A320")}, capacity_lookup) == []
    assert detect_aircraft_changes({"K": ("A320", None)}, capacity_lookup) == []
    assert detect_aircraft_changes({"K": ("A320", "A320")}, capacity_lookup) == []


def test_aircraft_change_empty_input():
    assert detect_aircraft_changes(None, capacity_lookup) == []
    assert detect_aircraft_changes({}, capacity_lookup) == []


# --------------------------------------------------------------------
# Orkestrasyon - YASAK 2
# --------------------------------------------------------------------

def test_never_returns_empty_list(calc):
    reasons = run_all("security", [departure(8, 0)], calc)
    assert len(reasons) >= 1


def test_quiet_window_yields_exactly_normal(calc):
    """Hiçbir eşik geçilmezse TEK madde: Normal operasyonel yoğunluk."""
    reasons = run_all("security", [departure(8, 0, aircraft="A320")], calc)
    assert len(reasons) == 1
    assert reasons[0].code == "normal"
    assert reasons[0].message == "Normal operasyonel yoğunluk"
    assert reasons[0].severity == "info"


def test_normal_not_added_when_another_reason_fires(calc):
    flights = [
        departure(8, 0, aircraft="B77W", key="A"),
        departure(8, 5, aircraft="A388", key="B"),
    ]
    reasons = run_all("security", flights, calc)
    codes = {r.code for r in reasons}
    assert "widebody" in codes
    assert "normal" not in codes


def test_all_codes_are_from_the_nine_detectors(calc):
    """YASAK 2: serbest metin/jenerik kod üretilemez."""
    flights = [
        departure(8, 0, aircraft="B77W", location="international", delay=40, key="A"),
        departure(8, 5, aircraft="A388", location="international", delay=60, key="B"),
        arrival(8, 6, location="international", key="C"),
        arrival(8, 7, location="international", key="D"),
        arrival(8, 8, location="international", key="E"),
        arrival(8, 9, location="international", key="F"),
        arrival(8, 10, location="international", key="G"),
    ]
    period = flights + [
        departure(8, 9, status="cancelled", key="CX"),
        departure(8, 9, status="diverted", key="DV"),
    ]
    reasons = run_all(
        "passport", flights, calc,
        all_period_flights=period,
        historical_baseline=2,
        rho=0.95,
        aircraft_changes={"A": ("A320", "B77W")},
        capacity_of_icao=capacity_lookup,
    )
    codes = {r.code for r in reasons}
    assert codes <= ALLOWED_CODES
    # Bu pencerede birden fazla neden birlikte tetikleniyor (kombinasyon)
    assert {"clustering", "widebody", "delay_compression", "utilization",
            "intl_share", "arrival_bank", "cancellation", "aircraft_change",
            "diversion"} <= codes


def test_security_process_excludes_passport_only_reasons(calc):
    """Neden 4, 5, 6 security çıktısında ASLA görünmez."""
    flights = [arrival(8, i, location="international", key=f"A{i}") for i in range(10)]
    reasons = run_all("security", flights, calc, rho=0.99)
    codes = {r.code for r in reasons}
    assert "utilization" not in codes
    assert "intl_share" not in codes
    assert "arrival_bank" not in codes


def test_detected_reason_to_dict_shape():
    reason = DetectedReason("normal", "info", "Normal operasyonel yoğunluk", 0.0)
    assert reason.to_dict() == {
        "code": "normal",
        "severity": "info",
        "message": "Normal operasyonel yoğunluk",
        "metric_value": 0.0,
    }
