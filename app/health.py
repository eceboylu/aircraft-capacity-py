from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

DEFAULT_STALE_THRESHOLD_SECONDS = 900
STALE_THRESHOLD_SECONDS = int(
    os.environ.get("HEALTH_STALE_THRESHOLD_SECONDS", DEFAULT_STALE_THRESHOLD_SECONDS)
)

_STATUS_ROW_ID = 1


class WorkerStatus(Base):

    __tablename__ = "worker_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_successful_refresh_at: Mapped[datetime] = mapped_column(DateTime)


def record_successful_refresh(session, when: datetime | None = None) -> None:
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


def _source_mode() -> tuple[str, bool]:
    if os.environ.get("AIRLABS_API_KEY"):
        try:
            from .queue.ingestion.airlabs_client import tracked_airports_from_env
            tracked_airports_from_env()
        except Exception:
            return "airlabs", False
        return "airlabs", True

    if os.environ.get("QUEUE_LOCAL_SOURCE_MODE", "").strip().lower() == "generated":
        return "generated_local_fixture", False
    return "bundled_sample_file", False


def _newest_prediction_at(session) -> datetime | None:
    from .queue.models import QueuePrediction  # noqa: PLC0415 - döngüsel import'tan kaçınmak için modül-seviyesinde DEĞİL, burada import edilir

    return session.execute(select(func.max(QueuePrediction.calculated_at))).scalar_one_or_none()


def build_health_report(session) -> dict:
    last = get_last_successful_refresh(session)
    newest_prediction = _newest_prediction_at(session)
    now = datetime.now(timezone.utc)

    source_mode, source_live_ingestion_configured = _source_mode()

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
        "source_live_ingestion_configured": source_live_ingestion_configured,
    }
