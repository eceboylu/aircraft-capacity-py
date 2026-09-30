"""
ADIM (2 Günlük Rolling Retention) - kullanıcı talebi: "2 günde bir 2 gün
öncekiler silinsin ama airport ölçekleri ve gişe/lane/capacity-icao
belirleme kısmı kalacak". `purge_stale_operational_data()` SADECE flight/
queue hesaplama verisine dokunur, airport/scale/config referans
tablolarına HİÇ DOKUNMAZ.
"""
from datetime import date, datetime

from app.queue.audit import purge_stale_operational_data
from app.queue.models import Airport, AirportScaleConfig, Flight, QueuePrediction


def _flight(flight_key: str, when: datetime, airport_iata: str = "IST") -> Flight:
    return Flight(
        flight_key=flight_key,
        airport_iata=airport_iata,
        direction="departure",
        location="domestic",
        requires_passport=True,
        dep_scheduled_utc=when,
        status="scheduled",
    )


def _prediction(process: str, window_start: datetime, operational_date: date) -> QueuePrediction:
    return QueuePrediction(
        airport_iata="IST",
        process=process,
        window_start=window_start,
        window_end=window_start,
        operational_date=operational_date,
        flight_count=1,
        expected_passengers=100,
        utilization=0.5,
        estimated_wait_minutes=5.0,
        risk="LOW",
        reasons="[]",
        confidence=1.0,
        calculated_at=window_start,
    )


def test_flights_older_than_keep_days_are_deleted_newer_kept(db_session):
    session = db_session
    today = date(2026, 10, 1)
    session.add_all([
        _flight("OLD1", datetime(2026, 9, 29, 10, 0)),   # 2 gün önce -> SİLİNMELİ
        _flight("OLD2", datetime(2026, 9, 28, 10, 0)),   # 3 gün önce -> SİLİNMELİ
        _flight("KEEP1", datetime(2026, 9, 30, 10, 0)),  # dün -> KALMALI
        _flight("KEEP2", datetime(2026, 10, 1, 10, 0)),  # bugün -> KALMALI
    ])
    session.commit()

    result = purge_stale_operational_data(session, keep_days=2, today=today)

    remaining = {f.flight_key for f in session.query(Flight).all()}
    assert remaining == {"KEEP1", "KEEP2"}
    assert result["flights"] == 2


def test_predictions_older_than_keep_days_are_deleted(db_session):
    session = db_session
    today = date(2026, 10, 1)
    session.add_all([
        _prediction("passport_arr", datetime(2026, 9, 29, 14, 0), date(2026, 9, 29)),
        _prediction("passport_arr", datetime(2026, 9, 30, 14, 0), date(2026, 9, 30)),
        _prediction("passport_arr", datetime(2026, 10, 1, 14, 0), date(2026, 10, 1)),
    ])
    session.commit()

    purge_stale_operational_data(session, keep_days=2, today=today)

    remaining_dates = {p.operational_date for p in session.query(QueuePrediction).all()}
    assert remaining_dates == {date(2026, 9, 30), date(2026, 10, 1)}


def test_airport_scale_and_config_reference_data_never_touched(db_session):
    """Kullanıcının açık talebi: 'airport ölçekleri ve gişe lane kısmı
    kalacak' - bu fonksiyon o tablolara HİÇ dokunmamalı, ne kadar eski
    olursa olsun."""
    session = db_session
    session.add(Airport(iata_code="IST", icao_code="LTFM", scale="mega"))
    session.add(AirportScaleConfig(
        scale="mega", departure_passport_servers=30, arrival_passport_servers=30,
        domestic_security_lanes=30, international_security_lanes=20,
    ))
    session.add(_flight("VERYOLD", datetime(2020, 1, 1, 0, 0)))
    session.commit()

    purge_stale_operational_data(session, keep_days=2, today=date(2026, 10, 1))

    assert session.query(Airport).filter_by(iata_code="IST").one().scale == "mega"
    assert session.query(AirportScaleConfig).filter_by(scale="mega").count() == 1
    assert session.query(Flight).filter_by(flight_key="VERYOLD").count() == 0


def test_keep_days_boundary_exact_cutoff_date_is_kept_not_deleted(db_session):
    """keep_days=2, bugün=1 Ekim -> cutoff=30 Eylül: 30 Eylül'ün KENDİSİ
    kalmalı (>= cutoff), sadece 29 Eylül ve öncesi silinmeli."""
    session = db_session
    session.add_all([
        _flight("BOUNDARY", datetime(2026, 9, 30, 0, 0, 1)),
        _flight("JUST_BEFORE", datetime(2026, 9, 29, 23, 59, 59)),
    ])
    session.commit()

    purge_stale_operational_data(session, keep_days=2, today=date(2026, 10, 1))

    remaining = {f.flight_key for f in session.query(Flight).all()}
    assert remaining == {"BOUNDARY"}


def test_default_keep_days_is_two_when_not_specified(db_session):
    session = db_session
    session.add_all([
        _flight("TWO_DAYS_AGO", datetime(2026, 9, 29, 10, 0)),
        _flight("YESTERDAY", datetime(2026, 9, 30, 10, 0)),
    ])
    session.commit()

    result = purge_stale_operational_data(session, today=date(2026, 10, 1))

    remaining = {f.flight_key for f in session.query(Flight).all()}
    assert remaining == {"YESTERDAY"}
    assert result["flights"] == 1
