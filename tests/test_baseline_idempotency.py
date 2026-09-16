"""
MADDE 3 doğrulaması - 15 dakikalık baseline observation idempotency.

İki seviyede test edilir:
  1. baseline.record_observation() - defter (BaselineObservation) +
     havuz (HistoricalFlightCount) seviyesinde tekillik.
  2. engine.record_baseline_observations() / run_predictions() -
     açık/kapalı pencere kararı, `now` enjeksiyonu.

Bellek içi SQLite kullanılır; gerçek dosya veya üretim verisi yoktur.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.baseline import get_baseline, record_observation
from app.queue.constants import PROCESS_PASSPORT, PROCESS_SECURITY
from app.queue.engine import (
    WindowPrediction,
    record_baseline_observations,
    run_predictions,
)
from app.queue.models import BaselineObservation, Flight, HistoricalFlightCount

from .factories import BASE_DAY, MockCapacityResolver, arrival, at, departure


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


def make_prediction(
    airport="AAA",
    process=PROCESS_SECURITY,
    window_start=None,
    window_minutes=15,
    flight_count=7,
) -> WindowPrediction:
    window_start = window_start or at(18, 0)
    return WindowPrediction(
        airport_iata=airport,
        process=process,
        window_start=window_start,
        window_end=window_start + timedelta(minutes=window_minutes),
        flight_count=flight_count,
        expected_passengers=flight_count * 100,
        baseline_ratio=None,
        utilization=None,
        estimated_wait_minutes=None,
        risk="LOW",
        confidence=0.8,
    )


def sample_size(session, airport, process, hour, day) -> int:
    row = session.execute(
        select(HistoricalFlightCount).where(
            HistoricalFlightCount.airport_iata == airport,
            HistoricalFlightCount.process == process,
            HistoricalFlightCount.hour_of_day == hour,
            HistoricalFlightCount.day_of_week == day,
        )
    ).scalar_one_or_none()
    return row.sample_size if row else 0


def observation_count(session) -> int:
    return session.scalar(select(func.count()).select_from(BaselineObservation))


# --------------------------------------------------------------------
# record_observation() - defter + havuz seviyesi
# --------------------------------------------------------------------

def test_same_window_recorded_once_even_after_many_calls(session):
    """Kural 2: aynı closed window 100 kez çağrılsa bile 1 observation."""
    window_start = at(18, 0)
    for _ in range(100):
        record_observation(
            session, "AAA", PROCESS_SECURITY, window_start,
            hour_of_day=18, day_of_week=window_start.weekday(),
            flight_count=10,
        )

    assert observation_count(session) == 1
    assert sample_size(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 1
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 10.0


def test_repeated_call_does_not_shift_average(session):
    """İlk çağrı ortalamayı belirler; sonraki tekrarlar hiç etkilemez."""
    window_start = at(18, 0)
    first = record_observation(
        session, "AAA", PROCESS_SECURITY, window_start,
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=10,
    )
    # Aynı pencere farklı bir flight_count ile "tekrar" gönderilse bile
    # (örn. yanlışlıkla stale veriyle çağrılsa) havuz DEĞİŞMEMELİ.
    second = record_observation(
        session, "AAA", PROCESS_SECURITY, window_start,
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=999,
    )
    assert first == 10.0
    assert second == 10.0
    assert sample_size(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 1


def test_different_window_start_same_hour_bucket_both_count(session):
    """
    Farklı tarihlerin aynı (saat, haftanın günü) havuzuna katkısı ayrı
    ayrı sayılmalı - defter window_start bazlı, havuz hour/day bazlı.
    """
    week1 = at(18, 0)                      # Pazartesi 2026-09-14
    week2 = week1 + timedelta(days=7)      # bir sonraki Pazartesi 18:00

    record_observation(
        session, "AAA", PROCESS_SECURITY, week1,
        hour_of_day=18, day_of_week=week1.weekday(), flight_count=10,
    )
    record_observation(
        session, "AAA", PROCESS_SECURITY, week2,
        hour_of_day=18, day_of_week=week2.weekday(), flight_count=20,
    )

    assert observation_count(session) == 2
    assert sample_size(session, "AAA", PROCESS_SECURITY, 18, week1.weekday()) == 2
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, week1.weekday()) == 15.0


def test_same_airport_different_process_are_independent(session):
    """Kural 5: aynı airport+window, farklı process -> ayrı observation."""
    window_start = at(18, 0)
    record_observation(
        session, "AAA", PROCESS_SECURITY, window_start,
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=10,
    )
    record_observation(
        session, "AAA", PROCESS_PASSPORT, window_start,
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=4,
    )

    assert observation_count(session) == 2
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 10.0
    assert get_baseline(session, "AAA", PROCESS_PASSPORT, 18, window_start.weekday()) == 4.0


def test_different_airport_same_process_same_window_are_independent(session):
    """Kural 6: aynı process+window, farklı airport -> ayrı observation."""
    window_start = at(18, 0)
    record_observation(
        session, "AAA", PROCESS_SECURITY, window_start,
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=10,
    )
    record_observation(
        session, "BBB", PROCESS_SECURITY, window_start,
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=30,
    )

    assert observation_count(session) == 2
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 10.0
    assert get_baseline(session, "BBB", PROCESS_SECURITY, 18, window_start.weekday()) == 30.0


def test_duplicate_insert_at_db_level_does_not_crash_or_double_count(session):
    """
    Kural 10 (concurrent/duplicate insert): defter satırı BAŞKA bir yol
    tarafından (ör. eşzamanlı bir refresh) zaten yazılmış gibi simüle
    edilir. record_observation() bunu IntegrityError olarak yakalayıp
    sessizce no-op yapmalı; session bozulmadan kullanılabilir kalmalı.
    """
    window_start = at(18, 0)
    session.add(BaselineObservation(
        airport_iata="AAA", process=PROCESS_SECURITY, window_start=window_start,
    ))
    session.commit()

    # Havuza hiç dokunulmamış olmalı (defter yazıldı ama havuz boş).
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) is None

    result = record_observation(
        session, "AAA", PROCESS_SECURITY, window_start,
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=10,
    )

    assert result == 10.0        # havuzda satır yoktu -> "mevcut ortalama" flight_count'un kendisi
    assert observation_count(session) == 1     # ikinci bir defter satırı açılmadı
    assert sample_size(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 0

    # Session rollback sonrası hâlâ sağlıklı - farklı bir pencereye yazabiliyor.
    record_observation(
        session, "AAA", PROCESS_SECURITY, window_start + timedelta(minutes=15),
        hour_of_day=18, day_of_week=window_start.weekday(), flight_count=5,
    )
    assert observation_count(session) == 2


# --------------------------------------------------------------------
# record_baseline_observations() - açık/kapalı pencere
# --------------------------------------------------------------------

def test_open_window_is_never_recorded(session):
    """Kural 3: window_end henüz gelmemişse baseline'a hiç yazılmaz."""
    window_start = at(18, 0)                       # pencere 18:00-18:15
    prediction = make_prediction(window_start=window_start)
    now = at(18, 7)                                # 18:07 - hâlâ açık

    recorded = record_baseline_observations(session, [prediction], now=now)

    assert recorded == 0
    assert observation_count(session) == 0
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) is None


def test_closed_window_is_recorded(session):
    """18:16'da 18:00-18:15 penceresi kapanmış sayılır ve baseline'a girer."""
    window_start = at(18, 0)
    prediction = make_prediction(window_start=window_start)
    now = at(18, 16)

    recorded = record_baseline_observations(session, [prediction], now=now)

    assert recorded == 1
    assert observation_count(session) == 1
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 7.0


def test_window_boundary_exact_close_is_recorded(session):
    """now == window_end (tam 18:15) -> kapalı sayılır (yarı-açık aralık kuralıyla tutarlı)."""
    window_start = at(18, 0)
    prediction = make_prediction(window_start=window_start)
    now = at(18, 15)

    recorded = record_baseline_observations(session, [prediction], now=now)
    assert recorded == 1


def test_window_boundary_one_minute_before_close_is_not_recorded(session):
    window_start = at(18, 0)
    prediction = make_prediction(window_start=window_start)
    now = at(18, 14)

    recorded = record_baseline_observations(session, [prediction], now=now)
    assert recorded == 0


def test_same_closed_window_refreshed_five_times_yields_one_observation(session):
    """Kural 2+3 birlikte: kapandıktan sonra 5+ kez refresh -> 1 observation."""
    window_start = at(18, 0)
    now = at(18, 20)

    for _ in range(5):
        prediction = make_prediction(window_start=window_start, flight_count=7)
        record_baseline_observations(session, [prediction], now=now)

    assert observation_count(session) == 1
    assert sample_size(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 1
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, window_start.weekday()) == 7.0


def test_window_transitions_from_open_to_closed_across_refreshes(session):
    """
    18:07 refresh -> açık, yazılmaz.
    18:16 refresh -> kapanmış, yazılır.
    Aynı senaryo dokümandaki örnekle birebir.
    """
    window_start = at(18, 0)
    prediction = make_prediction(window_start=window_start)

    early = record_baseline_observations(session, [prediction], now=at(18, 7))
    assert early == 0
    assert observation_count(session) == 0

    late = record_baseline_observations(session, [prediction], now=at(18, 16))
    assert late == 1
    assert observation_count(session) == 1


def test_mixed_open_and_closed_windows_in_same_batch(session):
    """Aynı refresh turunda hem kapalı hem açık pencere olabilir; sadece kapalı olan yazılır."""
    closed = make_prediction(window_start=at(17, 45), flight_count=5)
    open_ = make_prediction(window_start=at(18, 0), flight_count=9)
    now = at(18, 5)          # 17:45-18:00 kapandı, 18:00-18:15 hâlâ açık

    recorded = record_baseline_observations(session, [closed, open_], now=now)

    assert recorded == 1
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 17, closed.window_start.weekday()) == 5.0
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 18, open_.window_start.weekday()) is None


# --------------------------------------------------------------------
# run_predictions() ile uçtan uca - gerçek uçuş verisiyle
# --------------------------------------------------------------------

def store(db, mock_flights):
    columns = {c.name for c in Flight.__table__.columns}
    for mock in mock_flights:
        values = {
            name: value for name, value in vars(mock).items() if name in columns
        }
        db.add(Flight(**values))
    db.commit()


def test_run_predictions_skips_baseline_for_open_window(session):
    store(session, [departure(18, 0, key="D1", number="1", aircraft="A320")])
    resolver = MockCapacityResolver()

    # security_arrival_buffer_minutes=45 -> effective_time 17:15, pencere 17:15-17:30
    result = run_predictions(session, resolver, now=at(17, 20))

    assert result["predictions"] >= 1
    assert observation_count(session) == 0


def test_run_predictions_records_baseline_for_closed_window(session):
    """
    ADIM 6D-2 HOURLY MIGRATION: departure(18,0) domestic -> security
    buffer 45dk -> effective_time=17:15 -> saatlik pencere [17:00-18:00).
    Bu pencere ESKİDEN (15dk) 17:45'te çoktan kapanmıştı; ŞİMDİ (60dk)
    18:00'da kapanıyor - `now` buna göre güncellendi.
    """
    store(session, [departure(18, 0, key="D1", number="1", aircraft="A320")])
    resolver = MockCapacityResolver()

    result = run_predictions(session, resolver, now=at(18, 1))

    assert result["predictions"] >= 1
    assert observation_count(session) >= 1


def test_run_predictions_five_refreshes_of_closed_window_yield_one_observation(session):
    """
    ADIM (Security Domestic/International Split): domestic departure
    artık İKİ security sürecini besliyor (birleşik `security` +
    `security_dom`) - her biri KENDİ BaselineObservation defterine
    yazıyor, bu yüzden idempotent 1 gözlem/süreç x 2 süreç = 2 (1
    DEĞİL). Her sürecin KENDİ İÇİNDE hâlâ idempotent olduğu (5 kez
    çağrılsa da süreç başına TEK gözlem) asıl doğrulanan şey.
    """
    store(session, [departure(18, 0, key="D1", number="1", aircraft="A320")])
    resolver = MockCapacityResolver()
    now = at(19, 0)     # pencere kesinlikle kapanmış

    for _ in range(5):
        run_predictions(session, resolver, now=now)

    assert observation_count(session) == 2


def test_run_predictions_default_now_uses_real_clock(session):
    """
    `now` verilmezse gerçek saat kullanılır (domain_now()). Test uçuşları
    geçmiş bir tarihte (BASE_DAY) olduğu için pencereleri her zaman
    kapalıdır - varsayılan davranış da baseline'ı besler.
    """
    assert BASE_DAY < datetime.now(timezone.utc).replace(tzinfo=None)
    store(session, [departure(9, 0, key="D1", number="1", aircraft="A320")])
    resolver = MockCapacityResolver()

    run_predictions(session, resolver)

    assert observation_count(session) >= 1


# --------------------------------------------------------------------
# ADIM 5E-5 - `expire_on_commit` lokal geçici değişikliği restore ediliyor mu?
# --------------------------------------------------------------------

def test_expire_on_commit_is_restored_after_normal_completion(session):
    """
    Test A: `record_baseline_observations()` çağrılmadan önce
    `session.expire_on_commit` True ise, işlem NORMAL tamamlandıktan
    sonra da True kalmalı - global sessionmaker ayarı DEĞİŞMEDİ, sadece
    fonksiyonun kendi ömrü boyunca geçici olarak False'a çekildi.
    """
    assert session.expire_on_commit is True

    prediction = make_prediction(window_start=at(18, 0))
    record_baseline_observations(session, [prediction], now=at(18, 16))

    assert session.expire_on_commit is True


def test_expire_on_commit_is_restored_even_if_record_observation_raises(session, monkeypatch):
    """
    Test B: `record_observation()` beklenmeyen (IntegrityError DIŞINDA)
    bir hata fırlatsa bile, `finally` bloğu sayesinde
    `session.expire_on_commit` eski değerine geri dönmeli - global
    session durumu kalıcı olarak bozulmamalı.
    """
    import app.queue.engine as engine_mod

    def broken_record_observation(*args, **kwargs):
        raise RuntimeError("kasıtlı test hatası")

    monkeypatch.setattr(engine_mod, "record_observation", broken_record_observation)

    assert session.expire_on_commit is True
    prediction = make_prediction(window_start=at(18, 0))

    with pytest.raises(RuntimeError):
        record_baseline_observations(session, [prediction], now=at(18, 16))

    assert session.expire_on_commit is True


def test_expire_on_commit_false_session_is_restored_to_false_not_forced_true(session):
    """
    Restore işlemi HER ZAMAN True'ya değil, ÇAĞRI ÖNCESİNDEKİ gerçek
    değere dönmeli - eğer çağıran taraf zaten `expire_on_commit=False`
    ile bir session kullanıyorsa, bu tercih ezilmemeli.
    """
    session.expire_on_commit = False
    prediction = make_prediction(window_start=at(18, 0))

    record_baseline_observations(session, [prediction], now=at(18, 16))

    assert session.expire_on_commit is False
