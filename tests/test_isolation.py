"""
Madde 5.10/5.11 - airport isolation ve process isolation regresyonu.
"""
from datetime import datetime

from app.queue.config import default_config
from app.queue.constants import PROCESS_SECURITY_DOMESTIC
from app.queue.core.event_queue import simulate_passport
from app.queue.domain.demand import DemandCalculator
from app.queue.domain.flows import passport_arrival_flights, passport_departure_flights
from app.queue.engine import predict_airport

from .conftest import FakeCapacityResult, FakeResolver, make_arrival, make_departure

WHEN = datetime(2026, 3, 10, 9, 0)


# --- 5.10: Airport isolation --------------------------------------------

def test_airport_isolation_no_shared_state_between_calls():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)

    ist_flight = make_departure(when=WHEN, location="domestic", airport_iata="IST")

    ist_predictions = predict_airport("IST", [ist_flight], default_config("IST"), demand)
    # SAW hiç uçuş vermiyor - IST çağrısı (aynı process, önce çalıştı)
    # SAW'ın hesabına HİÇ SIZMAMALI.
    saw_predictions = predict_airport("SAW", [], default_config("SAW"), demand)

    assert all(p.airport_iata == "IST" for p in ist_predictions)
    assert saw_predictions == []

    ist_domestic = [p for p in ist_predictions if p.process == PROCESS_SECURITY_DOMESTIC]
    assert any(p.flight_count > 0 for p in ist_domestic)


def test_airport_isolation_identical_flights_different_airports_independent():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)

    ist_flight = make_departure(when=WHEN, location="domestic", airport_iata="IST")
    saw_flight = make_departure(when=WHEN, location="domestic", airport_iata="SAW")

    ist_predictions = predict_airport("IST", [ist_flight], default_config("IST"), demand)
    saw_predictions = predict_airport("SAW", [saw_flight], default_config("SAW"), demand)

    ist_domestic = [p for p in ist_predictions if p.process == PROCESS_SECURITY_DOMESTIC]
    saw_domestic = [p for p in saw_predictions if p.process == PROCESS_SECURITY_DOMESTIC]

    assert sum(p.expected_passengers for p in ist_domestic) == sum(
        p.expected_passengers for p in saw_domestic
    )
    assert all(p.airport_iata == "IST" for p in ist_predictions)
    assert all(p.airport_iata == "SAW" for p in saw_predictions)


# --- 5.11: Process isolation ---------------------------------------------

def test_domestic_departure_excluded_from_departure_passport():
    flight = make_departure(when=WHEN, location="domestic")
    assert flight not in passport_departure_flights([flight])


def test_international_arrival_excluded_from_departure_passport():
    flight = make_arrival(when=WHEN, location="international")
    assert flight not in passport_departure_flights([flight])
    assert flight in passport_arrival_flights([flight])


def test_departure_and_arrival_passport_pools_are_independent_resources():
    """
    Departure/Arrival passport ayrı server pool ise, arrival havuzundaki
    ağır bir backlog departure havuzunun servis zamanlarını HİÇ
    ETKİLEMEMELİ (ve tam tersi) - core/event_queue.py:simulate_passport
    zaten iki BAĞIMSIZ simulate_fifo_queue çağrısı yapıyor.
    """
    departure_arrivals = [(datetime(2026, 3, 10, 10, 0), 5.0)]
    # Arrival havuzu son derece aşırı yüklü - eğer havuzlar paylaşılsaydı
    # bu, departure'ın sunucularını da bloke ederdi.
    arrival_arrivals = [(datetime(2026, 3, 10, 9, 0), 500.0)]

    result = simulate_passport(
        departure_arrivals,
        arrival_arrivals,
        departure_server_count=2,
        arrival_server_count=2,
        service_time_minutes=1.5,
    )

    departure_events = result["departure"]
    assert departure_events
    max_wait = max(e.wait_minutes for e in departure_events)

    # Paylaşılan havuz olsaydı wait saatler mertebesinde olurdu (500
    # kişilik arrival backlog'u önce bitirmek gerekirdi); bağımsız
    # havuzda departure kendi 5 kişisini ~birkaç dakikada işler.
    assert max_wait < 5.0
