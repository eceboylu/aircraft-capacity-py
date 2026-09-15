"""
MADDE 1 - Ana kapasite servisi

KATMAN SIRASI:
  1) aircraft_capacity
     (yolcu_ucaklari.json + yedek)
  2) aircraft_capacity_family
     (kategori/önek fallback, genel havacılık dahil)
  3) Hiçbiri yoksa: sabit varsayılan (150) +
     ATOMIC upsert ile unknown_aircraft_types'a kayıt
     (dosya yazma yok, race condition yok)
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AircraftCapacity,
    AircraftCapacityFamily,
    UnknownAircraftType,
)

DEFAULT_CAPACITY = 150


@dataclass
class CapacityResult:
    icao: str
    airline: str | None
    capacity: int
    source: str
    confidence: str
    counts_toward_passenger_total: bool


class AircraftCapacityService:

    # Verilen uçak kodundan kapasiteyi çözümle/bul. 
    
    def __init__(self, session: Session):
        self.session = session

    def resolve(
        self,
        icao_code: str | None,
        airline_iata: str | None = None
    ) -> CapacityResult:

        code = (icao_code or "").strip().upper()
        airline = (airline_iata or "").strip().upper() or None

        if code == "":
            return CapacityResult(
                code,
                airline,
                DEFAULT_CAPACITY,
                "unknown_default",
                "low",
                True
            )

        # Katman 1: aircraft_capacity
        cached = self.session.execute(
            select(AircraftCapacity)
            .where(AircraftCapacity.icao_code == code)
        ).scalar_one_or_none()

        if cached is not None:
            return CapacityResult(
                code,
                airline,
                cached.capacity,
                cached.source,
                cached.confidence,
                True
            )

        # Katman 2: Kategori/önek fallback
        families = self.session.execute(
            select(AircraftCapacityFamily)
            .order_by(
                func.length(AircraftCapacityFamily.icao_prefix).desc()
            )
        ).scalars().all()

        for family in families:
            if code.startswith(family.icao_prefix):
                counts = bool(family.counts_toward_passenger_total)
                confidence = (
                    "high"
                    if family.category == "general_aviation"
                    else "low"
                )

                if not counts:
                    return CapacityResult(
                        code,
                        airline,
                        0,
                        "general_aviation_excluded",
                        confidence,
                        False
                    )

                self._flag_unknown(
                    code,
                    f"family_fallback:{family.category}"
                )

                return CapacityResult(
                    code,
                    airline,
                    family.default_capacity,
                    "family_fallback",
                    confidence,
                    True
                )

        # Katman 3: Varsayılan + atomic log
        self._flag_unknown(code, "no_match")

        return CapacityResult(
            code,
            airline,
            DEFAULT_CAPACITY,
            "unknown_default",
            "low",
            True
        )

    def _flag_unknown(self, code: str, reason: str) -> None:
        """
        Atomic upsert - dosya oku/değiştir/yaz YOK.

        Önce increment dene, satır yoksa oluşturmayı dene.
        Aynı anda başka bir process oluşturduysa
        (gerçek race condition) increment'e geri dön.
        """

        now = datetime.now(timezone.utc)

        updated = (
            self.session.query(UnknownAircraftType)
            .filter_by(icao_code=code)
            .update(
                {
                    UnknownAircraftType.seen_count:
                        UnknownAircraftType.seen_count + 1,
                    UnknownAircraftType.last_seen_at: now,
                }
            )
        )

        self.session.commit()

        if updated == 0:
            try:
                self.session.add(
                    UnknownAircraftType(
                        icao_code=code,
                        name=None,
                        reason=reason,
                        seen_count=1,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )

                self.session.commit()

            except IntegrityError:
                self.session.rollback()

                (
                    self.session.query(UnknownAircraftType)
                    .filter_by(icao_code=code)
                    .update(
                        {
                            UnknownAircraftType.seen_count:
                                UnknownAircraftType.seen_count + 1,
                            UnknownAircraftType.last_seen_at: now,
                        }
                    )
                )

                self.session.commit()