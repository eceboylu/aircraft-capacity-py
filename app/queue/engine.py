"""
AŞAMA 5 + 6 + 7 + 8 - Tahmin motoru (orkestrasyon).

Saf katmanları (scoring, detector, demand, flows) tek bir akışta
birleştirir:

    uçuş listesi
      -> süreç filtresi (security / passport)
      -> 15 dk pencereler
      -> skor (security: yoğunluk sinyali, passport: Erlang-C)
      -> 9 neden tespiti
      -> confidence
      -> QueuePrediction upsert

ÇOK-HAVALİMANLI: Hesap her zaman TEK bir havalimanının uçuş listesi
üzerinde çalışır. `run_predictions` bu tek-havalimanı hesabını
sistemdeki her havalimanı için ayrı ayrı tekrarlar; havalimanları
birbirinin uçuşunu, config'ini veya baseline'ını görmez.

Motorun çekirdeği (`predict_window`, `predict_airport`) veritabanına
DOKUNMAZ - baseline, uçak değişikliği ve config dışarıdan verilir.
Veritabanı bağlantısı sadece `run_predictions` ve yardımcılarındadır.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select

from .baseline import get_baseline, record_observation
from .config import AirportConfigView, get_configs
from .constants import (
    DEMAND_WINDOW_MINUTES,
    NO_BASELINE_MESSAGE,
    PASSPORT_OVERLOAD_MESSAGE,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    REASON_CAPACITY_EXCEEDED,
    REASON_NO_BASELINE,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
)
from .core.scoring import (
    confidence_score,
    passport_queue_model,
    security_density_score,
)
from .domain.demand import DemandCalculator, effective_time, flights_in_window
from .domain.flows import passport_flights, security_flights
from .models import Flight, QueuePrediction
from .reasons.detector import DetectedReason, detect_reasons

PROCESSES = (PROCESS_SECURITY, PROCESS_PASSPORT)

# Hangi sürecin hangi uçuşlarla beslendiği (AŞAMA 2).
_PROCESS_FLIGHTS = {
    PROCESS_SECURITY: security_flights,
    PROCESS_PASSPORT: passport_flights,
}


@dataclass
class WindowPrediction:
    """Tek bir (havalimanı, süreç, pencere) için sonuç."""

    airport_iata: str
    process: str
    window_start: datetime
    window_end: datetime
    flight_count: int
    expected_passengers: int
    baseline_ratio: float | None
    utilization: float | None
    estimated_wait_minutes: float | None
    risk: str
    confidence: float
    reasons: list[DetectedReason] = field(default_factory=list)

    def reasons_as_dicts(self) -> list[dict]:
        return [r.to_dict() for r in self.reasons]

    def reasons_json(self) -> str:
        return json.dumps(self.reasons_as_dicts(), ensure_ascii=False)


def floor_to_window(
    moment: datetime, window_minutes: int = DEMAND_WINDOW_MINUTES
) -> datetime:
    """Zamanı içinde bulunduğu pencerenin başlangıcına yuvarlar."""
    minute = (moment.minute // window_minutes) * window_minutes
    return moment.replace(minute=minute, second=0, microsecond=0)


def window_starts(
    flights, window_minutes: int = DEMAND_WINDOW_MINUTES
) -> list[datetime]:
    """
    Uçuşların düştüğü pencerelerin başlangıçları (sıralı, tekrarsız).

    Boş pencere için satır üretilmez - hiç uçuş olmayan saatlere
    tahmin yazmanın anlamı yok, tablo da gereksiz şişer.
    """
    starts = set()
    for flight in flights:
        moment = effective_time(flight)
        if moment is not None:
            starts.add(floor_to_window(moment, window_minutes))
    return sorted(starts)


def window_label(window_start: datetime, window_end: datetime) -> str:
    return f"{window_start:%H:%M}-{window_end:%H:%M}"


def _scoring_notes(score: dict) -> list[DetectedReason]:
    """
    AŞAMA 5'in kendi şeffaflık notlarını DetectedReason'a çevirir.

    Bunlar 9 tespit fonksiyonuna EK bir neden kaynağı değildir;
    skorun neden "UNKNOWN" ya da "CRITICAL" olduğunu açıklarlar.
    Serbest metin üretilmez, sadece bu iki sabit mesaj kullanılır.
    """
    notes = []
    for message in score.get("reasons", []):
        if message == NO_BASELINE_MESSAGE:
            notes.append(DetectedReason(
                code=REASON_NO_BASELINE,
                severity=SEVERITY_INFO,
                message=NO_BASELINE_MESSAGE,
                metric_value=0.0,
            ))
        elif message == PASSPORT_OVERLOAD_MESSAGE:
            notes.append(DetectedReason(
                code=REASON_CAPACITY_EXCEEDED,
                severity=SEVERITY_CRITICAL,
                message=PASSPORT_OVERLOAD_MESSAGE,
                metric_value=score.get("utilization") or 0.0,
            ))
    return notes


def predict_window(
    airport_iata: str,
    process: str,
    window_start: datetime,
    process_flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    historical_baseline: float | None,
    aircraft_match_rate: float,
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
) -> WindowPrediction:
    """
    Tek pencere hesabı. Veritabanına dokunmaz.

    process_flights : bu havalimanının, bu süreci besleyen uçuşları
                      (AŞAMA 2 filtresinden geçmiş)
    """
    window_end = window_start + timedelta(minutes=window_minutes)

    # Talep hesabına giren uçuşlar (iptal/diverted hariç).
    window_flights = flights_in_window(
        process_flights, window_start, window_minutes
    )
    # Neden tespiti için aynı pencere, iptal ve yönlendirmeler dahil.
    window_all = flights_in_window(
        process_flights, window_start, window_minutes, include_excluded=True
    )

    if process == PROCESS_PASSPORT:
        score = passport_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes
        )
        rho = score["utilization"]
    else:
        score = security_density_score(
            window_flights, historical_baseline, demand.passenger_demand
        )
        # Security'de kapasite verisi yok; utilization da dakika da üretilmez.
        rho = None

    window_changes = None
    if aircraft_changes:
        keys = {getattr(f, "flight_key", None) for f in window_all}
        window_changes = {
            key: value
            for key, value in aircraft_changes.items()
            if key in keys
        }

    reasons = _scoring_notes(score)
    reasons.extend(detect_reasons(
        airport_iata=airport_iata,
        process=process,
        window_flights=window_flights,
        all_period_flights=window_all,
        historical_baseline=historical_baseline,
        rho=rho,
        config=config,
        seat_capacity_fn=demand.seat_capacity,
        demand_fn=demand.passenger_demand,
        window_label=window_label(window_start, window_end),
        aircraft_changes=window_changes,
        capacity_of_icao=demand.capacity_of_icao,
    ))

    confidence = confidence_score(
        window_flights=window_flights,
        historical_baseline_available=historical_baseline is not None,
        aircraft_match_rate=aircraft_match_rate,
        config_is_default=config.is_default,
    )

    return WindowPrediction(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        window_end=window_end,
        flight_count=score["flight_count"],
        expected_passengers=score["expected_passengers"],
        baseline_ratio=score.get("baseline_ratio"),
        utilization=rho,
        estimated_wait_minutes=score["estimated_wait_minutes"],
        risk=score["risk"],
        confidence=confidence,
        reasons=reasons,
    )


def predict_airport(
    airport_iata: str,
    flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    baseline_fn=None,
    aircraft_match_rate: float | None = None,
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
) -> list[WindowPrediction]:
    """
    Bir havalimanının iki süreci için tüm pencereler.

    baseline_fn : (process, window_start) -> float | None
                  Geçmiş veri yoksa None döndürmeli; SAHTE BASELINE
                  ÜRETİLMEZ.
    """
    if aircraft_match_rate is None:
        aircraft_match_rate = flight_match_rate(flights)

    predictions: list[WindowPrediction] = []

    for process in PROCESSES:
        relevant = _PROCESS_FLIGHTS[process](flights)
        for start in window_starts(relevant, window_minutes):
            baseline = baseline_fn(process, start) if baseline_fn else None
            predictions.append(predict_window(
                airport_iata=airport_iata,
                process=process,
                window_start=start,
                process_flights=relevant,
                config=config,
                demand=demand,
                historical_baseline=baseline,
                aircraft_match_rate=aircraft_match_rate,
                aircraft_changes=aircraft_changes,
                window_minutes=window_minutes,
            ))

    return predictions


def flight_match_rate(flights) -> float:
    """
    Uçak tipi çözülebilen uçuşların oranı (AŞAMA 7 girdisi).
    Uçuş yoksa 0.0 - sahte %100 üretilmez.
    """
    if not flights:
        return 0.0
    matched = sum(
        1 for f in flights if getattr(f, "aircraft_icao", None) is not None
    )
    return matched / len(flights)


# --------------------------------------------------------------------
# Veritabanı bağlantılı katman
# --------------------------------------------------------------------

def airport_codes(session) -> list[str]:
    """Veritabanındaki uçuşların geçtiği havalimanları."""
    rows = session.execute(
        select(Flight.airport_iata).distinct().order_by(Flight.airport_iata)
    ).scalars().all()
    return [code for code in rows if code]


def flights_of_airport(session, airport_iata: str) -> list[Flight]:
    """Sadece bu havalimanının uçuşları - başka havalimanı karışmaz."""
    return list(session.execute(
        select(Flight).where(Flight.airport_iata == airport_iata)
    ).scalars().all())


def _db_baseline_fn(session, airport_iata: str):
    """Baseline okumasını (süreç, pencere) çiftine bağlar."""
    def lookup(process: str, window_start: datetime) -> float | None:
        return get_baseline(
            session,
            airport_iata=airport_iata,
            process=process,
            hour_of_day=window_start.hour,
            day_of_week=window_start.weekday(),
        )
    return lookup


def persist_predictions(session, predictions: list[WindowPrediction]) -> dict:
    """
    QueuePrediction UPSERT: (havalimanı, süreç, pencere) başına TEK
    satır. Aynı pencere tekrar hesaplanırsa satır güncellenir, yenisi
    açılmaz (YASAK 4).
    """
    now = datetime.now(timezone.utc)
    inserted = 0
    updated = 0

    for prediction in predictions:
        existing = session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == prediction.airport_iata,
                QueuePrediction.process == prediction.process,
                QueuePrediction.window_start == prediction.window_start,
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = QueuePrediction(
                airport_iata=prediction.airport_iata,
                process=prediction.process,
                window_start=prediction.window_start,
            )
            session.add(existing)
            inserted += 1
        else:
            updated += 1

        existing.window_end = prediction.window_end
        existing.flight_count = prediction.flight_count
        existing.expected_passengers = prediction.expected_passengers
        existing.baseline_ratio = prediction.baseline_ratio
        existing.utilization = prediction.utilization
        existing.estimated_wait_minutes = prediction.estimated_wait_minutes
        existing.risk = prediction.risk
        existing.reasons = prediction.reasons_json()
        existing.confidence = prediction.confidence
        existing.calculated_at = now

    session.commit()
    return {"inserted": inserted, "updated": updated}


def prune_stale_predictions(
    session, airport_iata: str, keep: set[tuple[str, datetime]]
) -> int:
    """
    Bu havalimanının, yeni hesapta ARTIK ÜRETİLMEYEN tahmin satırlarını
    siler.

    Gerekçe: bir uçuş gecikince efektif saati değişir ve başka bir
    pencereye taşınır. Upsert yalnızca yeni pencereyi günceller; eski
    pencere dokunulmadan kalır ve orada olmayan bir talebi göstermeye
    devam eder. QueuePrediction bir CURRENT-STATE tablosudur, uçuş
    tablosundan deterministik olarak türetilir - dolayısıyla yeni
    hesapta yer almayan satır tanım gereği bayattır.

    Uzun vadeli hafıza bu tabloda değil, historical_flight_counts
    tablosundadır; buradan silinen satır geçmiş bilgisini götürmez.
    """
    rows = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == airport_iata
        )
    ).scalars().all()

    removed = 0
    for row in rows:
        if (row.process, row.window_start) not in keep:
            session.delete(row)
            removed += 1

    if removed:
        session.commit()
    return removed


def record_baseline_observations(
    session, predictions: list[WindowPrediction]
) -> int:
    """
    Pencere uçuş sayılarını clustering baseline'ına ekler.

    ÖNEMLİ: Tahminler hesaplandıktan SONRA çağrılır; yoksa pencere
    kendi baseline'ını beslemiş olur. Baseline birimi bu yüzden
    "o saat/gün için bir 15 dk penceredeki ortalama uçuş sayısı"dır -
    security ratio'su da aynı birimle karşılaştırılır.
    """
    for prediction in predictions:
        record_observation(
            session,
            airport_iata=prediction.airport_iata,
            process=prediction.process,
            hour_of_day=prediction.window_start.hour,
            day_of_week=prediction.window_start.weekday(),
            flight_count=prediction.flight_count,
        )
    return len(predictions)


def run_predictions(
    session,
    resolver,
    airports: list[str] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    update_baseline: bool = True,
) -> dict:
    """
    Sistemdeki HER havalimanı için tahminleri üretir ve kaydeder.

    resolver : Madde 1'in AircraftCapacityService örneği. Bu servis
               burada DEĞİŞTİRİLMEZ, sadece kullanılır.
    """
    from .ingestion.refresh import aircraft_changes_for_airport

    codes = airports if airports is not None else airport_codes(session)
    configs = get_configs(session, codes)

    total: list[WindowPrediction] = []
    per_airport: dict[str, int] = {}
    pruned = 0

    for code in codes:
        flights = flights_of_airport(session, code)
        if not flights:
            per_airport[code] = 0
            continue

        predictions = predict_airport(
            airport_iata=code,
            flights=flights,
            config=configs[code],
            demand=DemandCalculator(resolver),
            baseline_fn=_db_baseline_fn(session, code),
            aircraft_changes=aircraft_changes_for_airport(session, code),
            window_minutes=window_minutes,
        )
        per_airport[code] = len(predictions)
        total.extend(predictions)

        pruned += prune_stale_predictions(
            session,
            code,
            {(p.process, p.window_start) for p in predictions},
        )

    written = persist_predictions(session, total)

    if update_baseline:
        record_baseline_observations(session, total)

    return {
        "airports": per_airport,
        "predictions": len(total),
        "pruned": pruned,
        **written,
    }
