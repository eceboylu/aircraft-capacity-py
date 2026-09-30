"""
ADIM (Performance - Gerçek Bulk INSERT) - `audit.py:_bulk_add()` artık
`session.add_all()` (ORM, satır-satır INSERT) yerine `session.execute(
insert(Model), [dict,...])` (SQLAlchemy 2.0 `insertmanyvalues`)
kullanıyor. Bu testler row count/field values/created_at default'unun
BİREBİR korunduğunu, gerçek MySQL'e karşı doğruluyor.
"""
from datetime import datetime, timezone

from app.queue.audit import _bulk_add
from app.queue.models import QueueCountryRoutingAudit


def _make_row(**overrides):
    base = dict(
        run_id="run-bulk-1",
        airport_iata="XXX",
        calculation_date=None,
        country_code="TR",
        country_name="Türkiye",
        direction="departure",
        flight_type="international",
        schengen_status="non_schengen",
        flight_count=5,
        passenger_count=123.5,
        passport_required_count=3,
        passport_bypass_count=2,
        security_dom_passengers=10.0,
        passport_dep_passengers=20.0,
        security_intl_passengers=30.0,
        passport_arr_passengers=40.0,
    )
    base.update(overrides)
    return QueueCountryRoutingAudit(**base)


def test_bulk_add_writes_exact_row_count(db_session):
    session = db_session
    rows = [_make_row(airport_iata=f"A{i}") for i in range(7)]
    _bulk_add(session, rows)
    session.commit()

    count = session.query(QueueCountryRoutingAudit).filter_by(run_id="run-bulk-1").count()
    assert count == 7


def test_bulk_add_preserves_field_values_exactly(db_session):
    session = db_session
    row = _make_row(
        airport_iata="ZRH", flight_count=181, passenger_count=32346.0,
        passport_required_count=100, passport_bypass_count=81,
    )
    _bulk_add(session, [row])
    session.commit()

    stored = session.query(QueueCountryRoutingAudit).filter_by(
        run_id="run-bulk-1", airport_iata="ZRH"
    ).one()
    assert stored.flight_count == 181
    assert stored.passenger_count == 32346.0
    assert stored.passport_required_count == 100
    assert stored.passport_bypass_count == 81
    assert stored.security_intl_passengers == 30.0  # precision korunmuş
    assert stored.country_code == "TR"
    assert stored.direction == "departure"


def test_bulk_add_nullable_fields_stay_null(db_session):
    session = db_session
    row = _make_row(airport_iata="NULLTEST", country_code=None, country_name=None, calculation_date=None)
    _bulk_add(session, [row])
    session.commit()

    stored = session.query(QueueCountryRoutingAudit).filter_by(
        run_id="run-bulk-1", airport_iata="NULLTEST"
    ).one()
    assert stored.country_code is None
    assert stored.country_name is None
    assert stored.calculation_date is None


def test_bulk_add_applies_created_at_default_when_unset(db_session):
    """`created_at` instance'ta HİÇ set edilmemişse (None), `_bulk_add`
    ORM'un normalde flush anında yapacağı `utcnow()` çağrısının AYNISINI
    yapmalı - satır NULL created_at ile kalmamalı."""
    session = db_session
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    row = _make_row(airport_iata="CRTTEST")
    assert row.created_at is None  # constructor'da hiç set edilmedi
    _bulk_add(session, [row])
    session.commit()
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    stored = session.query(QueueCountryRoutingAudit).filter_by(
        run_id="run-bulk-1", airport_iata="CRTTEST"
    ).one()
    assert stored.created_at is not None
    # MySQL DATETIME (fractional-seconds YOK) saniyeye yuvarlıyor - bu
    # yüzden 1 saniyelik tolerans (mikrosaniye hassasiyeti test EDİLMİYOR,
    # sadece "gerçekten şimdi/civarında" set edildiği doğrulanıyor).
    from datetime import timedelta
    assert before - timedelta(seconds=1) <= stored.created_at <= after + timedelta(seconds=1)


def test_bulk_add_empty_list_is_noop(db_session):
    session = db_session
    _bulk_add(session, [])  # exception atmamalı
    session.commit()
    count = session.query(QueueCountryRoutingAudit).filter_by(run_id="run-bulk-1").count()
    assert count == 0


def test_bulk_add_multiple_chunks_beyond_chunk_size(db_session):
    """AUDIT_INSERT_CHUNK_SIZE=500 sınırını aşan bir liste, birden fazla
    chunk/statement'a bölünse bile TÜM satırlar eksiksiz yazılmalı."""
    session = db_session
    rows = [_make_row(airport_iata=f"CHUNK{i}", flight_count=i) for i in range(1200)]
    _bulk_add(session, rows)
    session.commit()

    count = session.query(QueueCountryRoutingAudit).filter_by(run_id="run-bulk-1").count()
    assert count == 1200
    sample = session.query(QueueCountryRoutingAudit).filter_by(
        run_id="run-bulk-1", airport_iata="CHUNK999"
    ).one()
    assert sample.flight_count == 999
