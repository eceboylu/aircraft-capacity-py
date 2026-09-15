"""
ADIM 5G-1 - Pipeline seviyesinde multi-airport ingestion failure
isolation kanıtı.

ADIM 5F audit'inde bulunan davranış: `build_source_a()` birden fazla
havalimanını işlerken bir havalimanının `fetch_schedules()` çağrısı
kalıcı olarak başarısız olursa (retry'ler tükenip `AirLabsError`
fırlatınca), exception `provide(direction)`'ı tamamen kesiyor ve
`pipeline.load_flight_rows()`'un direction-seviyeli
`except (OSError, ValueError)` bloğu o yöndeki TÜM havalimanlarının
(başarılı olanlar dahil) kayıtlarını atıyordu.

Bu dosya, gerçek zinciri (sahte SADECE `urllib.request.urlopen`)
kullanarak IST/SAW/ADB üç havalimanlı bir turda, SAW'ın kalıcı olarak
başarısız olmasının IST/ADB'nin verisini SİLMEDİĞİNİ kanıtlar:

    sahte urlopen (SAW için her zaman 500, diğerleri için gerçek mock)
      -> app.queue.ingestion.airlabs_client (GERÇEK, retry/backoff dahil)
      -> app.queue.ingestion.airlabs_client.build_source_a (GERÇEK, ADIM 5G-1 fix)
      -> app.queue.pipeline.run() (GERÇEK)

`tests/airlabs_mock_source.py` (ADIM 4'ün ortak mock altyapısı)
DEĞİŞTİRİLMEDEN kullanılır; SAW'a özel arıza SADECE bu test
dosyasındaki yerel bir `urlopen` sarmalayıcısıyla enjekte edilir.
"""

import io
import os
import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.ingestion import airlabs_client
from app.queue.ingestion.airports_import import import_airports
from app.queue.models import Flight
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

from . import airlabs_mock_source

REAL_AIRPORTS_SQL = os.path.join(
    os.path.dirname(__file__), "..", "data", "flight_airports.sql"
)


def _http_error(code: int = 500) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://airlabs.co/api/v9/schedules",
        code=code,
        msg="synthetic failure (test)",
        hdrs=None,
        fp=io.BytesIO(b""),
    )


def _install_flaky_source(monkeypatch, failing_airport: str):
    """
    ADIM 4 mock altyapısını (IST/SAW/ADB schedules fixture'ları)
    kurar, sonra SAdece `failing_airport` için urlopen'i her zaman
    HTTP 500 fırlatacak şekilde SARAR - diğer havalimanları normal
    mock verisini almaya devam eder.

    Gerçek retry/backoff mekanizmasını yavaşlatmamak için
    `airlabs_client.time.sleep` no-op'a çevrilir (testin kendisi
    3 deneme x backoff süresince gerçekten beklemesin diye - retry
    SAYISI/DAVRANIŞI değişmiyor, sadece gerçek uyku atlanıyor).
    """
    source = airlabs_mock_source.install(monkeypatch)
    monkeypatch.setattr(airlabs_client.time, "sleep", lambda seconds: None)

    real_urlopen = source.urlopen

    def flaky_urlopen(request, timeout=None):
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        airport = (query.get("dep_iata") or query.get("arr_iata") or [None])[0]
        if airport == failing_airport:
            raise _http_error(500)
        return real_urlopen(request, timeout)

    monkeypatch.setattr(urllib.request, "urlopen", flaky_urlopen)
    return source


def _run_pipeline_isolated_db(monkeypatch):
    """
    Bellek içi, test-izole bir veritabanına karşı GERÇEK
    `pipeline.run()`'ı çalıştırır (bkz. test_e2e_refresh.py'deki AYNI
    desen) ve (summary, session_factory) döner.
    """
    test_engine = create_engine("sqlite://")
    test_session_local = sessionmaker(bind=test_engine)

    monkeypatch.setattr(
        pipeline_module, "init_db",
        lambda drop_first=False: Base.metadata.create_all(test_engine),
    )
    monkeypatch.setattr(pipeline_module, "get_session", lambda: test_session_local())

    Base.metadata.create_all(test_engine)
    seed_session = test_session_local()
    import_airports(seed_session, REAL_AIRPORTS_SQL)
    seed_verified_dataset(seed_session)
    seed_curated_fallback(seed_session)
    seed_family_and_ga(seed_session)
    seed_session.close()

    source_a = airlabs_client.build_source_a(airlabs_mock_source.AIRPORTS)  # IST, SAW, ADB
    source_b = airlabs_client.build_source_b()

    summary = pipeline_module.run(source_a=source_a, source_b=source_b)
    return summary, test_session_local


def test_pipeline_level_partial_airport_failure_preserves_other_airports(monkeypatch):
    """
    IST success, SAW GERÇEKTEN başarısız (500, retry tükeniyor), ADB
    success - tek bir `pipeline.run()` turunda (departure VE arrival
    ikisi de SAW için başarısız olur, çünkü `build_source_a` her iki
    yön için de çağrılır).

    ESKİ (buggy) davranışta bu senaryo `flights_parsed=0` üretirdi
    (IST/ADB dahil TÜMÜ kaybolurdu, bkz. ADIM 5F audit + stress test).
    YENİ davranışta IST ve ADB'nin uçuşları KORUNUR, sadece SAW'ınkiler
    eksik kalır.
    """
    _install_flaky_source(monkeypatch, failing_airport="SAW")

    summary, session_factory = _run_pipeline_isolated_db(monkeypatch)

    # Ana iddia: eski buggy davranış (flights_parsed=0) artık GERÇEKLEŞMİYOR.
    assert summary["flights_parsed"] > 0

    check_session = session_factory()
    airports_in_db = set(
        check_session.execute(select(Flight.airport_iata).distinct()).scalars().all()
    )
    check_session.close()

    assert "IST" in airports_in_db
    assert "ADB" in airports_in_db
    assert "SAW" not in airports_in_db


def test_pipeline_level_failure_does_not_crash_process(monkeypatch):
    """
    Kısmi havalimanı hatası `pipeline.run()`'ı EXCEPTION ile
    patlatmaz - normal bir summary sözlüğü döner (main()'in exit-code
    kararı zaten `failed_airports`'u kritik saymıyor, bu test sadece
    run()'ın kendisinin ayakta kaldığını doğrular).
    """
    _install_flaky_source(monkeypatch, failing_airport="SAW")

    summary, _ = _run_pipeline_isolated_db(monkeypatch)

    assert isinstance(summary, dict)
    assert "flights_parsed" in summary
