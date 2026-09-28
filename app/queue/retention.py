
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


FLIGHT_RETENTION_DAYS = _env_int("FLIGHT_RETENTION_DAYS", "2")
FLIGHT_EVENT_RETENTION_DAYS = _env_int("FLIGHT_EVENT_RETENTION_DAYS", "2")
QUEUE_PREDICTION_RETENTION_DAYS = _env_int("QUEUE_PREDICTION_RETENTION_DAYS", "2")
BASELINE_OBSERVATION_RETENTION_DAYS = _env_int("BASELINE_OBSERVATION_RETENTION_DAYS", "30")

RETENTION_ENABLED = _env_bool("RETENTION_ENABLED", "false")
RETENTION_DRY_RUN = _env_bool("RETENTION_DRY_RUN", "true")

BATCH_SIZE = 500


@dataclass
class RetentionReport:

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
    rows = session.execute(select(Flight)).scalars().all()
    return [
        f.id for f in rows
        if (t := canonical_flight_time(f)) is not None and t < cutoff
    ]


def _expired_flight_event_ids(session, cutoff: datetime) -> list[int]:
    return list(session.execute(
        select(FlightEvent.id).where(FlightEvent.detected_at < cutoff)
    ).scalars().all())


def _expired_queue_prediction_candidates(session, cutoff: datetime) -> list[QueuePrediction]:
    return list(session.execute(
        select(QueuePrediction).where(QueuePrediction.window_start < cutoff)
    ).scalars().all())


def _expired_baseline_observation_ids(session, cutoff: datetime) -> list[int]:
    return list(session.execute(
        select(BaselineObservation.id).where(BaselineObservation.window_start < cutoff)
    ).scalars().all())


def _historical_contribution_committed(session, prediction: QueuePrediction) -> bool:
    existing = session.execute(
        select(BaselineObservation.id).where(
            BaselineObservation.airport_iata == prediction.airport_iata,
            BaselineObservation.process == prediction.process,
            BaselineObservation.window_start == prediction.window_start,
        )
    ).scalar_one_or_none()
    return existing is not None


def _delete_in_batches(session, model, ids: list[int]) -> int:
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
