"""
MADDE 1 - Ana kapasite servisi

KATMAN SIRASI (MADDE 4 ile güncellendi):
  1) airline_fleet_seat_config - exact airline + ICAO
     (havayolunun o tipteki TEK varyantı)
  2) airline_fleet_seat_config - airline + ICAO filo ağırlıklı ortalaması
     (SUM(seats*fleet_count) / SUM(fleet_count))
  3) aircraft_capacity
     (yolcu_ucaklari.json + yedek)
  4) aircraft_capacity_family
     (kategori/önek fallback, genel havacılık dahil)
  5) Hiçbiri yoksa: sabit varsayılan (180) +
     ATOMIC upsert ile unknown_aircraft_types'a kayıt
     (dosya yazma yok, race condition yok)

Tek kapasite çözüm noktası burasıdır - queue tarafında ikinci bir
resolver YOKTUR, hepsi bu servisi enjekte edip kullanır.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AircraftCapacity,
    AircraftCapacityFamily,
    AirlineFleetSeatConfig,
    UnknownAircraftType,
)

DEFAULT_CAPACITY = 180


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

        # Katman 1-2: airline_fleet_seat_config (exact / weighted average)
        if airline:
            fleet_result = self._resolve_airline_fleet(code, airline)
            if fleet_result is not None:
                return fleet_result

        if code == "":
            return CapacityResult(
                code,
                airline,
                DEFAULT_CAPACITY,
                "unknown_default",
                "low",
                True
            )

        # Katman 3: aircraft_capacity
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

        # Katman 4: Kategori/önek fallback
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

        # Katman 5: Varsayılan + atomic log
        self._flag_unknown(code, "no_match")

        return CapacityResult(
            code,
            airline,
            DEFAULT_CAPACITY,
            "unknown_default",
            "low",
            True
        )

    def _resolve_airline_fleet(
        self, code: str, airline: str
    ) -> CapacityResult | None:
        """
        MADDE 4 - Havayoluna özel filo koltuk konfigürasyonu.
        """
        if code:
            rows = self.session.execute(
                select(AirlineFleetSeatConfig).where(
                    AirlineFleetSeatConfig.icao_code == code,
                    AirlineFleetSeatConfig.airline_iata == airline,
                )
            ).scalars().all()
        else:
            rows = self.session.execute(
                select(AirlineFleetSeatConfig).where(
                    AirlineFleetSeatConfig.airline_iata == airline,
                )
            ).scalars().all()

        if not rows:
            return None

        if code and len(rows) == 1:
            return CapacityResult(
                code,
                airline,
                rows[0].seats,
                "airline_fleet_exact",
                "high",
                True
            )

        weighted_rows = [row for row in rows if row.fleet_count]
        total_fleet = sum(row.fleet_count for row in weighted_rows)

        if total_fleet <= 0:
            return None

        weighted_seats = sum(
            row.seats * row.fleet_count for row in weighted_rows
        )

        return CapacityResult(
            code,
            airline,
            round(weighted_seats / total_fleet),
            "airline_fleet_weighted_average",
            "high",
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