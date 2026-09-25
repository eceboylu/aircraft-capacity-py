"""
ADIM (Operational Retention) - `genel-proje.md` Bölüm 6-21.

Raw operational tablolar (`Flight`, `FlightEvent`, `QueuePrediction`)
için 48 saatlik retention. 5 dakikalık refresh'ten (`app/worker.py`/
`app/queue/pipeline.py`) TAMAMEN BAĞIMSIZDIR - bu modül refresh/
prediction akışına HİÇ dokunmaz, sadece worker'ın kendi AYRI, kendi
try/except'i içindeki bir çağrısıyla (ayrı 48 saatlik cadence)
tetiklenir (bkz. `worker.py`).

Persistent tablolar (`Airport`, `AirportOperationalConfig`,
`AircraftCapacity`) BU MODÜLDE HİÇ SİLİNMEZ.

`HistoricalFlightCount` de HİÇ SİLİNMEZ - ama bunun sebebi "henüz
retention eklenmedi" DEĞİL, bu tablonun YAPISAL OLARAK zaten sınırsız
büyüyemiyor olması: UNIQUE constraint'i (airport_iata, process,
hour_of_day, day_of_week) - yani satır sayısı ZAMANLA değil, SADECE
(havalimanı x süreç x 24 saat x 7 gün) kombinasyon sayısıyla sınırlı,
sabit bir HAVUZ (`record_observation()` yeni satır EKLEMEZ, var olan
satırı running-average ile GÜNCELLER). Bu satırları yaşa göre silmek
storage kazandırmaz (tablo zaten sabit boyutlu) ama Neden 1
(clustering) baseline kalitesini GERÇEKTEN bozar - o (havalimanı,
süreç, saat, gün) dilimi için öğrenilmiş ortalama sıfırlanır. Bu
yüzden BİLİNÇLİ OLARAK retention dışında bırakıldı.

`BaselineObservation` ise GERÇEKTEN sınırsız büyür - UNIQUE constraint'i
(airport_iata, process, window_start) - `window_start` gerçek, hep
ilerleyen bir zaman damgası olduğu için her yeni pencere kalıcı bir
defter satırı ekler, asla eskisinin yerine geçmez. Bu tablonun TEK işi
"bu somut pencere HistoricalFlightCount havuzuna daha önce eklendi mi"
sorusunu yanıtlamak (Bölüm 12) - bir pencerenin katkısı HistoricalFlightCount'a
committed olduktan SONRA, o BaselineObservation satırının kendisi
retention açısından güvenle silinebilir: sildiğimiz şey sadece "bu
zaten sayıldı" bayrağıdır, katkının KENDİSİ (running average içinde)
HİÇ kaybolmaz. Tek risk: silinen bir pencere bir şekilde YENİDEN
ingest edilirse (bkz. Re-Ingest Loop Prevention - 48h horizon filtresi
zaten bunu engelliyor) çift sayılabilir - bu yüzden `BASELINE_
OBSERVATION_RETENTION_DAYS` varsayılanı (30 gün), 48 saatlik re-ingest
penceresinin ÇOK ÜSTÜNDE, geniş bir güvenlik payıyla seçildi.

Canonical zaman kuralı `domain/retention_time.py`'den gelir - flight_key
ile AYNI departure/arrival seçimi (Bölüm 3/13) - burada TEKRAR
YAZILMAZ.

Config (Bölüm 10): mevcut `app/db.py:DATABASE_URL` stiliyle AYNI -
modül importunda `os.environ.get(...)`. Destructive bir production
varsayılanı YOK: `RETENTION_ENABLED` varsayılan `false`, `RETENTION_
DRY_RUN` varsayılan `true` - ikisi de açıkça set edilmeden gerçek
DELETE HİÇ ÇALIŞMAZ (bkz. `cleanup_expired_operational_data()`
docstring'i).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from .domain.retention_time import canonical_flight_time
from .models import BaselineObservation, Flight, FlightEvent, QueuePrediction

logger = logging.getLogger(__name__)


def _env_int(name: str, default: str) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- Config (Bölüm 10) ------------------------------------------------
FLIGHT_RETENTION_DAYS = _env_int("FLIGHT_RETENTION_DAYS", "2")
FLIGHT_EVENT_RETENTION_DAYS = _env_int("FLIGHT_EVENT_RETENTION_DAYS", "2")
QUEUE_PREDICTION_RETENTION_DAYS = _env_int("QUEUE_PREDICTION_RETENTION_DAYS", "2")
# ADIM (Retention Strategy - BaselineObservation) - modül docstring'inin
# üstteki bölümüne bkz.: bu tablo GERÇEKTEN sınırsız büyüdüğü için
# (diğer üçünün AKSİNE) retention'a AYRI olarak eklendi. 30 gün, 48
# saatlik re-ingest-horizon penceresinin çok üstünde bilinçli bir
# güvenlik payı - çift sayım riski YAPISAL olarak imkansız hale gelene
# kadar satır silinmez.
BASELINE_OBSERVATION_RETENTION_DAYS = _env_int("BASELINE_OBSERVATION_RETENTION_DAYS", "30")

# Bölüm 10 - "unsafe destructive production default oluşturma": ikisi de
# GÜVENLİ tarafta varsayılan alır - retention açıkça enable edilmeden VE
# dry-run açıkça kapatılmadan hiçbir satır silinmez.
RETENTION_ENABLED = _env_bool("RETENTION_ENABLED", "false")
RETENTION_DRY_RUN = _env_bool("RETENTION_DRY_RUN", "true")

# Bölüm 18 - batch delete. 500: mevcut ingestion testlerinde/gerçek
# fixture'larda tipik refresh hacmi onlarca-yüzlerce satır - 500'lük
# batch tek iterasyonda biter; hacim büyürse (binlerce/milyonlarca satır)
# tek dev DELETE yerine güvenli, bellekte sınırlı adımlarla ilerler.
# Rastgele seçilmiş bir sayı değil: `ingestion/refresh.py:REFRESH_CHUNK_
# SIZE` (MySQL Performance Regression Fix) ile AYNI değer/AYNI gerekçe -
# id listesini tek `IN (...)` ifadesine güvenle sığdıracak, gereksiz
# yere büyük olmayan bir batch boyutu.
BATCH_SIZE = 500


@dataclass
class RetentionReport:
    """Tek bir `cleanup_expired_operational_data()` çağrısının sonucu."""

    dry_run: bool
    flight_cutoff: datetime
    flight_event_cutoff: datetime
    queue_prediction_cutoff: datetime
    baseline_observation_cutoff: datetime

    flight_candidates: int = 0
    flight_deleted: int = 0

    flight_event_candidates: int = 0
    flight_event_deleted: int = 0

    queue_prediction_candidates: int = 0
    queue_prediction_deleted: int = 0
    queue_prediction_skipped_uncommitted: int = 0

    baseline_observation_candidates: int = 0
    baseline_observation_deleted: int = 0

    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "flight_cutoff": self.flight_cutoff.isoformat(),
            "flight_event_cutoff": self.flight_event_cutoff.isoformat(),
            "queue_prediction_cutoff": self.queue_prediction_cutoff.isoformat(),
            "baseline_observation_cutoff": self.baseline_observation_cutoff.isoformat(),
            "flight_candidates": self.flight_candidates,
            "flight_deleted": self.flight_deleted,
            "flight_event_candidates": self.flight_event_candidates,
            "flight_event_deleted": self.flight_event_deleted,
            "queue_prediction_candidates": self.queue_prediction_candidates,
            "queue_prediction_deleted": self.queue_prediction_deleted,
            "queue_prediction_skipped_uncommitted": self.queue_prediction_skipped_uncommitted,
            "baseline_observation_candidates": self.baseline_observation_candidates,
            "baseline_observation_deleted": self.baseline_observation_deleted,
            "errors": self.errors,
        }


def _expired_flight_ids(session, cutoff: datetime) -> list[int]:
    """
    Bölüm 13 - canonical zaman (`domain/retention_time.py`, flight_key
    ile AYNI departure/arrival kuralı) `cutoff`'tan ESKİ olan Flight id'leri.

    Canonical zamanı None olan (scheduled bilinmeyen) satırlar HİÇ
    aday sayılmaz - Bölüm 14 (future/active safety): bilinmeyen bir
    zamanı "eski" varsayıp silmek YAPILMAZ.

    NOT: bu sorgu Python'da filtrelenir (SQL CASE/WHEN yerine) - AYNI
    desen `engine.py:flights_of_airport()`'un 48h usage-horizon
    filtresiyle TUTARLI (tek canonical fonksiyon, iki çağıran).
    """
    rows = session.execute(select(Flight)).scalars().all()
    return [
        f.id for f in rows
        if (t := canonical_flight_time(f)) is not None and t < cutoff
    ]


def _expired_flight_event_ids(session, cutoff: datetime) -> list[int]:
    """Bölüm 4 - FlightEvent retention timestamp'i `detected_at`."""
    return list(session.execute(
        select(FlightEvent.id).where(FlightEvent.detected_at < cutoff)
    ).scalars().all())


def _expired_queue_prediction_candidates(session, cutoff: datetime) -> list[QueuePrediction]:
    """Bölüm 5 - QueuePrediction retention timestamp'i `window_start` (`calculated_at` DEĞİL)."""
    return list(session.execute(
        select(QueuePrediction).where(QueuePrediction.window_start < cutoff)
    ).scalars().all())


def _expired_baseline_observation_ids(session, cutoff: datetime) -> list[int]:
    """
    ADIM (Retention Strategy - BaselineObservation) - retention
    timestamp'i `window_start` (QueuePrediction ile AYNI alan/ilke -
    "bu somut pencere ne zamandı", `recorded_at` DEĞİL). `window_start
    < cutoff` olan bir satır, o pencerenin katkısı HistoricalFlightCount'a
    ÇOK ÖNCE committed olmuş demektir (aksi halde zaten `record_
    observation()` bu satırı hiç YARATMAZDI - satır VARLIĞININ KENDİSİ
    "committed" kanıtıdır, `_historical_contribution_committed()`'ın
    QueuePrediction için yaptığı ayrı kontrole burada GEREK YOK).
    """
    return list(session.execute(
        select(BaselineObservation.id).where(BaselineObservation.window_start < cutoff)
    ).scalars().all())


def _historical_contribution_committed(session, prediction: QueuePrediction) -> bool:
    """
    Bölüm 12 - bir QueuePrediction satırının `HistoricalFlightCount`'a
    katkısı zaten `BaselineObservation`'a committed mi?

    `record_baseline_observations()`/`record_observation()` (engine.py/
    baseline.py) SADECE kapanmış pencereleri (`window_end <= now`)
    `BaselineObservation(airport_iata, process, window_start)` olarak
    işaretler (unique constraint - bkz. rapor Bölüm 12). Bu üçlü orada
    VARSA katkı GÜVENLE committed'dır - satır silinebilir. YOKSA (pencere
    hiç kapanmamış VEYA `record_baseline_observations` bu pencere için
    hiç çağrılmamış) satır SİLİNMEZ, bir sonraki cleanup cycle'a
    bırakılır (Bölüm 12: "row'u DELETE ETME. SKIP et.").
    """
    existing = session.execute(
        select(BaselineObservation.id).where(
            BaselineObservation.airport_iata == prediction.airport_iata,
            BaselineObservation.process == prediction.process,
            BaselineObservation.window_start == prediction.window_start,
        )
    ).scalar_one_or_none()
    return existing is not None


def _delete_in_batches(session, model, ids: list[int]) -> int:
    """Bölüm 18 - tek dev DELETE değil, `BATCH_SIZE`'lık parçalar halinde."""
    deleted = 0
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start:start + BATCH_SIZE]
        session.execute(
            model.__table__.delete().where(model.id.in_(batch))
        )
        session.commit()
        deleted += len(batch)
    return deleted


def cleanup_expired_operational_data(
    session,
    now: datetime | None = None,
    dry_run: bool | None = None,
) -> RetentionReport:
    """
    Bölüm 6-21 - `Flight`/`FlightEvent`/`QueuePrediction` için 48 saatlik
    (varsayılan `*_RETENTION_DAYS=2`) retention cleanup.

    dry_run : None ise modül-seviyesi `RETENTION_DRY_RUN` (varsayılan
              True) kullanılır - AÇIKÇA `False` verilmeden hiçbir satır
              SİLİNMEZ, sadece aday sayıları hesaplanır (Bölüm 11/16 -
              "shared DB'de SADECE inventory/dry-run/candidate
              calculation").

    Sınır (Bölüm 7): `timestamp < cutoff` -> expired; `timestamp ==
    cutoff` -> KORUNUR (sıkı `<`, `<=` DEĞİL). BaselineObservation için
    de AYNI sınır kuralı uygulanır.

    HistoricalFlightCount'a HİÇ yazılmaz, HİÇ silinmez - bu tablo
    YAPISAL olarak zaten sınırlı boyutlu (bkz. modül docstring'i),
    silmek storage kazandırmaz ama baseline kalitesini bozar.

    BaselineObservation ARTIK bu fonksiyonda retention'a tabi (bkz.
    modül docstring'i - "committed" kontrolüne GEREK yok, satırın
    varlığı zaten committed olduğunun kanıtı).

    Persistent tablolara (Airport, AirportOperationalConfig, Aircraft
    reference) bu fonksiyon hiç dokunmaz - import bile etmiyor.
    """
    now = now if now is not None else datetime.utcnow()
    dry_run = RETENTION_DRY_RUN if dry_run is None else dry_run

    flight_cutoff = now - timedelta(days=FLIGHT_RETENTION_DAYS)
    flight_event_cutoff = now - timedelta(days=FLIGHT_EVENT_RETENTION_DAYS)
    queue_prediction_cutoff = now - timedelta(days=QUEUE_PREDICTION_RETENTION_DAYS)
    baseline_observation_cutoff = now - timedelta(days=BASELINE_OBSERVATION_RETENTION_DAYS)

    report = RetentionReport(
        dry_run=dry_run,
        flight_cutoff=flight_cutoff,
        flight_event_cutoff=flight_event_cutoff,
        queue_prediction_cutoff=queue_prediction_cutoff,
        baseline_observation_cutoff=baseline_observation_cutoff,
    )

    # ADIM (Health/Retention Audit - Observability) - worker.py zaten
    # başarı/başarısızlığı loglar (bkz. run_forever() "retention cleanup
    # #%d ok/BAŞARISIZ") ama cleanup'ın NE ZAMAN BAŞLADIĞI ayrıca
    # görünür değildi - uzun süren/asılı kalan bir cleanup'ı "hiç
    # başlamadı" durumundan ayırt etmek için minimal, tek satırlık ek.
    logger.info(
        "retention cleanup başladı (dry_run=%s, flight_cutoff=%s, "
        "baseline_observation_cutoff=%s)",
        dry_run, flight_cutoff, baseline_observation_cutoff,
    )

    try:
        flight_ids = _expired_flight_ids(session, flight_cutoff)
        report.flight_candidates = len(flight_ids)
        if not dry_run and flight_ids:
            report.flight_deleted = _delete_in_batches(session, Flight, flight_ids)

        event_ids = _expired_flight_event_ids(session, flight_event_cutoff)
        report.flight_event_candidates = len(event_ids)
        if not dry_run and event_ids:
            report.flight_event_deleted = _delete_in_batches(session, FlightEvent, event_ids)

        prediction_candidates = _expired_queue_prediction_candidates(session, queue_prediction_cutoff)
        report.queue_prediction_candidates = len(prediction_candidates)
        eligible_ids = []
        for prediction in prediction_candidates:
            if _historical_contribution_committed(session, prediction):
                eligible_ids.append(prediction.id)
            else:
                report.queue_prediction_skipped_uncommitted += 1
        if not dry_run and eligible_ids:
            report.queue_prediction_deleted = _delete_in_batches(
                session, QueuePrediction, eligible_ids
            )

        baseline_observation_ids = _expired_baseline_observation_ids(session, baseline_observation_cutoff)
        report.baseline_observation_candidates = len(baseline_observation_ids)
        if not dry_run and baseline_observation_ids:
            report.baseline_observation_deleted = _delete_in_batches(
                session, BaselineObservation, baseline_observation_ids
            )
    except Exception as exc:  # noqa: BLE001 - Bölüm 9: cleanup hatası worker'ı/refresh'i ÖLDÜRMEZ
        session.rollback()
        report.errors.append(repr(exc))
        logger.exception("retention cleanup başarısız - rollback yapıldı, sonraki cycle'da tekrar denenecek")

    return report


def run_cleanup_cycle(now: datetime | None = None, dry_run: bool | None = None) -> dict:
    """
    ADIM (Retention Cleanup Schedule) - Bölüm 9: `worker.py:run_forever()`'ın
    48 saatlik cadence'inden çağrılan, KENDİ session yaşam döngüsünü
    yöneten sarmalayıcı - `pipeline.run()`'ın `init_db()`/`get_session()`/
    `finally: session.close()` deseniyle AYNI (mevcut, kanıtlanmış
    desen - yeni bir session yönetim şekli İCAT EDİLMEDİ).

    `RETENTION_ENABLED=false` (varsayılan) ise DB'ye HİÇ BAĞLANMAZ,
    `{"enabled": False}` döner - Bölüm 10: "destructive cleanup'ı
    production'da sessizce enable etme" kuralının somut uygulaması,
    yeni bir deployment hiçbir env var set edilmeden retention'ın
    FARKINDA bile olmaz.
    """
    if not RETENTION_ENABLED:
        return {"enabled": False}

    from ..db import get_session, init_db

    init_db()
    session = get_session()
    try:
        report = cleanup_expired_operational_data(session, now=now, dry_run=dry_run)
        return {"enabled": True, **report.as_dict()}
    finally:
        session.close()
