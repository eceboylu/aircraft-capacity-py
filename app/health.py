"""
ADIM (Health / Stale-Data Visibility) - production'da web server ayakta
olsa bile worker durmuşsa veya veri eskiyse bunu görünür kılmak için
minimal bir runtime-status kalıcılığı.

Queue matematiğinin (app/queue/*) - risk/wait/demand/show-up/arrival
release/capacity resolver - HESAPLAMA mantığına HİÇ dokunmaz, hiçbir
queue fonksiyonunu ÇAĞIRMAZ, hiçbir tabloya YAZMAZ (worker_status
DIŞINDA). `QueuePrediction`'ı SADECE salt-okunur `MAX(calculated_at)`
ile okur (bkz. `build_health_report` - ADIM Health Semantics Audit) -
"worker crashlemeden döndü" ile "prediction verisi gerçekten üretiliyor"
İKİ AYRI sinyaldir (worker.py'nin `pipeline.run()` başarılı dönmesi,
o cycle'da HERHANGİ bir airport için yeni prediction yazıldığını
GARANTİ ETMEZ - engine.py:persist_predictions() sadece kapsamdaki
pencereler için satır yazar); bu yüzden ikisi AYRI AYRI izlenir, worker
heartbeat'i "prediction üretimi de tazedir" anlamına GELMEZ varsayımı
YAPILMAZ.

Tamamen ayrı, TEK satırlık bir `worker_status` tablosu - minimal schema
eklentisi (Bölüm gereksinimi: "Schema değişikliği gerekiyorsa minimal
yap").

Process restart'ta bilgi KAYBOLMAZ - in-memory bir sayaç/değişken
DEĞİLDİR, her başarılı refresh'te DB'ye commit edilir (bkz.
`record_successful_refresh`, `app/worker.py:run_forever()`'dan
çağrılır).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

# Veri kaç saniye eskiyince "stale" sayılır. Refresh cadence 300s (5 dk,
# app/worker.py:REFRESH_INTERVAL_SECONDS) - 900s (=3x cadence) eşiği,
# ara sıra tek bir turun başarısız olup bir sonrakinde kendi kendine
# düzelmesini (worker'ın zaten tasarlanmış davranışı) YANLIŞ ALARMA
# çevirmeden, GERÇEK bir kesintiyi (üst üste ~3 turun tamamı başarısız)
# makul bir gecikmeyle yakalayan bir orta nokta. `HEALTH_STALE_THRESHOLD_
# SECONDS` ile override edilebilir - projedeki diğer ops-ayarlanabilir
# değerlerle (RETENTION_ENABLED vb.) AYNI env-var deseni.
DEFAULT_STALE_THRESHOLD_SECONDS = 900
STALE_THRESHOLD_SECONDS = int(
    os.environ.get("HEALTH_STALE_THRESHOLD_SECONDS", DEFAULT_STALE_THRESHOLD_SECONDS)
)

_STATUS_ROW_ID = 1


class WorkerStatus(Base):
    """
    TEK satırlık (id sabit=1) worker runtime durumu. Her başarılı
    pipeline cycle'ı sonunda UPSERT edilir - `app/worker.py`'nin KENDİ
    refresh/retry mantığına hiç karışmaz, sadece "en son ne zaman
    başarılı oldu"yu ayrı, salt-gözlemsel bir tabloda tutar.
    """

    __tablename__ = "worker_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_successful_refresh_at: Mapped[datetime] = mapped_column(DateTime)


def record_successful_refresh(session, when: datetime | None = None) -> None:
    """
    `app/worker.py:run_forever()`'ın HER başarılı `run()` çağrısından
    sonra çağırması beklenir. KENDİ commit'ini yapar - çağıran tarafın
    (worker) zaten kapatmış olduğu `pipeline.run()` session'ından
    TAMAMEN BAĞIMSIZ, yeni/ayrı bir DB round-trip'idir.
    """
    when = when if when is not None else datetime.now(timezone.utc)
    existing = session.get(WorkerStatus, _STATUS_ROW_ID)
    if existing is None:
        session.add(WorkerStatus(id=_STATUS_ROW_ID, last_successful_refresh_at=when))
    else:
        existing.last_successful_refresh_at = when
    session.commit()


def get_last_successful_refresh(session) -> datetime | None:
    row = session.get(WorkerStatus, _STATUS_ROW_ID)
    return row.last_successful_refresh_at if row else None


def _newest_prediction_at(session) -> datetime | None:
    """
    ADIM (Health Semantics Audit) - `QueuePrediction.calculated_at`'in
    global MAX'ı, salt-okunur tek bir `SELECT MAX(...)`. `persist_
    predictions()` (engine.py) her başarılı cycle'da, kapsamdaki HER
    pencere için `calculated_at = now` yazar (değer değişmese bile) -
    bu yüzden bu sinyal, worker heartbeat'inden (`WorkerStatus`) BAĞIMSIZ
    olarak "prediction ÜRETİMİ gerçekten en son ne zaman oldu"yu yanıtlar;
    ikisi normalde birbirine yakın hareket eder ama AYNI ŞEY DEĞİLDİR -
    `pipeline.run()` hata fırlatmadan dönüp heartbeat'i güncelleyebilir
    while bir airport/tüm airportlar için hiç yeni prediction satırı
    yazılmamış olabilir (ör. run_predictions()'ın kapsamındaki hiçbir
    pencerede flight yoksa) - ayrı izlenmezse bu durum health'te GÖRÜNMEZ
    kalırdı.
    """
    from .queue.models import QueuePrediction  # noqa: PLC0415 - döngüsel import'tan kaçınmak için modül-seviyesinde DEĞİL, burada import edilir

    return session.execute(select(func.max(QueuePrediction.calculated_at))).scalar_one_or_none()


def build_health_report(session) -> dict:
    """
    `GET /health`'in gövdesi. Salt-okunur: `WorkerStatus` (heartbeat) ve
    `QueuePrediction.calculated_at` (MAX, prediction üretim tazeliği)
    okur - ikisi de AYRI sinyal, hiçbiri diğerinin yerine geçmez (bkz.
    `_newest_prediction_at` docstring'i). Hiçbir tabloya YAZMAZ, hiçbir
    queue/prediction HESAPLAMA fonksiyonunu ÇAĞIRMAZ.

    status kararı - HER İKİ sinyal de "taze" olmalı, biri bile stale/eksik
    ise "healthy" DÖNMEZ:
      - DB'ye hiç erişilemiyorsa (çağıran taraf `session` alırken zaten
        patlar - bkz. server.py) -> "unhealthy" (bu fonksiyona hiç
        girilmez, çağıran taraf kendi except'inde bu durumu üretir).
      - Worker hiç başarılı refresh yapmamışsa VEYA hiç prediction
        satırı yoksa (ilk cycle henüz bitmemiş) -> "degraded" - DB ve
        web server'ın kendisi sağlıklı, sadece henüz veri yok; bu
        "unhealthy" (bozuk) değil, "henüz hazır değil" durumudur.
      - Worker heartbeat `STALE_THRESHOLD_SECONDS`'ı AŞTIYSA -> "degraded".
      - Prediction MAX(calculated_at) AYNI eşiği AŞTIYSA -> "degraded"
        (worker heartbeat taze olsa BİLE - Bölüm: "worker koşuyor ama
        prediction üretimi durmuş" senaryosu BURADA yakalanır).
      - Aksi halde (İKİSİ de taze) -> "healthy".

    `source_mode`/`source_live_ingestion_configured` SADECE bilgilendirici
    alanlardır, `status` kararını ETKİLEMEZ - bu projede `source_a`/
    `source_b` HER ZAMAN dosya-tabanlıdır (bkz. pipeline.py
    `file_source_a`/`file_source_b`); `app/queue/ingestion/airlabs_client.py`
    var ama hiçbir yerden import EDİLMİYOR (grep ile doğrulandı) - yani
    "source çok uzun süredir yeni veri getirmedi" diye bir canlı-feed
    kesinti sinyali ÜRETİLEMEZ/ÜRETİLMEMELİDİR (yoksayılan bir feed'i
    "stale" saymak yanlış alarm üretir). Bunun yerine hangi modda
    çalıştığı AÇIKÇA/dürüstçe raporlanır (Bölüm G: "gizlenmemeli").
    """
    last = get_last_successful_refresh(session)
    newest_prediction = _newest_prediction_at(session)
    now = datetime.now(timezone.utc)

    source_mode = (
        "generated_local_fixture"
        if os.environ.get("QUEUE_LOCAL_SOURCE_MODE", "").strip().lower() == "generated"
        else "bundled_sample_file"
    )

    def _as_aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    last_aware = _as_aware(last)
    newest_prediction_aware = _as_aware(newest_prediction)
    data_age_seconds = (now - last_aware).total_seconds() if last_aware is not None else None
    prediction_age_seconds = (
        (now - newest_prediction_aware).total_seconds() if newest_prediction_aware is not None else None
    )

    worker_stale = last_aware is None or data_age_seconds > STALE_THRESHOLD_SECONDS
    prediction_stale = newest_prediction_aware is None or prediction_age_seconds > STALE_THRESHOLD_SECONDS
    overall_stale = worker_stale or prediction_stale

    return {
        "status": "degraded" if overall_stale else "healthy",
        "db": "ok",
        "last_successful_refresh": last_aware.isoformat() if last_aware else None,
        "data_age_seconds": round(data_age_seconds, 1) if data_age_seconds is not None else None,
        "stale": worker_stale,
        "newest_prediction_at": newest_prediction_aware.isoformat() if newest_prediction_aware else None,
        "prediction_age_seconds": round(prediction_age_seconds, 1) if prediction_age_seconds is not None else None,
        "prediction_stale": prediction_stale,
        "source_mode": source_mode,
        "source_live_ingestion_configured": False,
    }
