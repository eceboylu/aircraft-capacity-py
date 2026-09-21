"""
ADIM (Production Auto-Refresh Architecture) - Section AC doğrulaması.

`app/worker.py:run_forever()`, GERÇEK `app/queue/pipeline.py:run()`'ı
çağırarak (mock/bypass EDİLMEDEN) `tests/incoming_2026_09_19/` incoming
batch'ini - hiçbir manuel `python -m app.queue.pipeline` komutu
OLMADAN - ingest edip prediction ürettiğini kanıtlar.

Gerçek `database.sqlite`/`data/*.json` HİÇ açılmadı - izole bir
in-memory SQLite + `pipeline.init_db`/`get_session` monkeypatch'i
kullanılır (bu oturumdaki diğer izole-DB testleriyle AYNI, kanıtlanmış
desen - bkz. `run_date_shift_replay.py`).
"""
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app import worker
from app.models import Base
from app.queue.ingestion.airports_import import import_airport_scales, import_airports
from app.queue.ingestion.sources import load_source_payload
from app.queue.models import Flight, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

INCOMING_DIR = Path(__file__).resolve().parent / "incoming_2026_09_19"
REAL_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
REAL_AIRPORTS_SQL = REAL_DATA_DIR / "flight_airports.sql"


@pytest.fixture()
def isolated_engine(monkeypatch):
    engine = create_engine("sqlite://")
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    seed_session = Session()
    import_airports(seed_session, str(REAL_AIRPORTS_SQL))
    import_airport_scales(
        seed_session,
        str(REAL_DATA_DIR / "buyuk_olcekli_havaalanlari.txt"),
        str(REAL_DATA_DIR / "orta_olcekli_havaalanlari.txt"),
        str(REAL_DATA_DIR / "kucuk_olcekli_havaalanlari.txt"),
    )
    seed_verified_dataset(seed_session)
    seed_curated_fallback(seed_session)
    seed_family_and_ga(seed_session)
    seed_session.commit()
    seed_session.close()

    monkeypatch.setattr(pipeline_module, "init_db", lambda drop_first=False: Base.metadata.create_all(engine))
    monkeypatch.setattr(pipeline_module, "get_session", lambda: Session())
    return engine


def _source_b_provider():
    return load_source_payload(str(INCOMING_DIR / "flights_live.json"))


def test_worker_run_forever_triggers_real_ingestion_without_manual_pipeline_command(isolated_engine):
    """Bölüm AC/10: `python -m app.queue.pipeline` HİÇ ÇAĞRILMADI - sadece worker.run_forever()."""
    Session = sessionmaker(bind=isolated_engine)
    before = Session().execute(select(Flight)).scalars().all()
    assert before == []   # ingestion'dan ÖNCE hiç flight yok

    def run_once():
        return pipeline_module.run(
            data_dir=str(INCOMING_DIR),
            source_b=_source_b_provider,
            now=datetime(2026, 9, 19, 20, 0),
        )

    completed = worker.run_forever(
        interval_seconds=300, run_fn=run_once, sleep_fn=lambda s: None, max_iterations=1,
    )
    assert completed == 1

    session = Session()
    flights = session.execute(select(Flight).where(Flight.airport_iata == "IST")).scalars().all()
    predictions = session.execute(select(QueuePrediction).where(QueuePrediction.airport_iata == "IST")).scalars().all()
    session.close()

    assert flights            # gerçek refresh_flights() ÇALIŞTI
    assert predictions        # gerçek run_predictions() ÇALIŞTI


def test_worker_second_cycle_does_not_duplicate_flights(isolated_engine):
    """Aynı incoming batch iki worker döngüsünden geçse bile flight sayısı İKİYE KATLANMAZ (upsert)."""
    Session = sessionmaker(bind=isolated_engine)

    def run_once():
        return pipeline_module.run(
            data_dir=str(INCOMING_DIR), source_b=_source_b_provider,
            now=datetime(2026, 9, 19, 20, 0),
        )

    worker.run_forever(interval_seconds=300, run_fn=run_once, sleep_fn=lambda s: None, max_iterations=1)
    session = Session()
    count_after_first = len(session.execute(select(Flight)).scalars().all())
    session.close()

    worker.run_forever(interval_seconds=300, run_fn=run_once, sleep_fn=lambda s: None, max_iterations=1)
    session = Session()
    count_after_second = len(session.execute(select(Flight)).scalars().all())
    session.close()

    assert count_after_first == count_after_second


def test_worker_survives_one_bad_cycle_then_real_ingestion_still_succeeds_next_cycle(isolated_engine):
    """Bölüm 7/8: ilk döngü hata verse bile worker ÖLMEZ, ikinci döngüde gerçek ingestion ÇALIŞIR."""
    Session = sessionmaker(bind=isolated_engine)
    calls = {"n": 0}

    def flaky_then_real():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simüle edilmiş geçici kaynak hatası")
        return pipeline_module.run(
            data_dir=str(INCOMING_DIR), source_b=_source_b_provider,
            now=datetime(2026, 9, 19, 20, 0),
        )

    completed = worker.run_forever(
        interval_seconds=300, run_fn=flaky_then_real, sleep_fn=lambda s: None, max_iterations=2,
    )
    assert completed == 1   # sadece 2. döngü başarılı

    session = Session()
    flights = session.execute(select(Flight).where(Flight.airport_iata == "SAW")).scalars().all()
    session.close()
    assert flights   # son (başarılı) döngü GERÇEKTEN ingest etti
