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
import logging

from sqlalchemy import select

from .baseline import get_baseline, get_passenger_baseline, record_observation
from .config import AirportConfigView, get_configs
from .constants import (
    DEMAND_WINDOW_MINUTES,
    EXCLUDED_STATUSES,
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

logger = logging.getLogger(__name__)

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
    # MADDE 7 - security'de baseline_ratio bu iki bileşenin ağırlıklı
    # ortalamasıdır. Passport'ta ikisi de None kalır.
    flight_ratio: float | None = None
    passenger_ratio: float | None = None
    reasons: list[DetectedReason] = field(default_factory=list)

    def reasons_as_dicts(self) -> list[dict]:
        return [r.to_dict() for r in self.reasons]

    def reasons_json(self) -> str:
        return json.dumps(self.reasons_as_dicts(), ensure_ascii=False)


def domain_now() -> datetime:
    """
    Uçuş alanlarıyla (dep/arr_*_utc, window_start/end) AYNI birimde:
    naive UTC.

    models.utcnow() tz-aware döner - last_refreshed_at/detected_at gibi
    audit kolonları için doğrudur. Ama pencere sınırları Kaynak A'dan
    gelen naive UTC zamanlardan türediği için (bkz. ingestion/sources.py:
    parse_utc) tz-aware bir değerle karşılaştırmak TypeError verir.
    Bu fonksiyon SADECE "pencere kapandı mı" gibi domain zaman
    karşılaştırmaları için kullanılır; test edilebilirlik için çağıran
    taraflarda `now` parametresiyle override edilebilir.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _bucket_flights_by_window(
    flights, window_minutes: int = DEMAND_WINDOW_MINUTES
) -> dict[datetime, list]:
    """
    ADIM 5E-2 - prediction lookup optimizasyonu.

    Her uçuşun `effective_time()`'ını TAM OLARAK BİR KEZ hesaplayıp
    `floor_to_window()` ile 15 dk pencere anahtarına göre gruplar.
    `effective_time`/`floor_to_window`'ın kendisi HİÇ DEĞİŞMEDİ - sadece
    kaç kez çağrıldıkları (önceden pencere sayısı kadar tekrar tekrar,
    şimdi uçuş başına bir kez).

    MATEMATİKSEL EŞDEĞERLİK (bkz. `flights_in_window` - DEĞİŞMEDİ,
    hâlâ mevcut/ayrı çağıranlar için kullanılabilir):

        floor_to_window(effective_time(flight)) == window_start
        ⟺ window_start <= effective_time(flight) < window_end

    (floor_to_window'ın tanımı gereği) - yani bu bucket'lama,
    `flights_in_window`'ın "bu pencereye girer mi" testiyle BİREBİR AYNI
    sonucu üretir.

    `effective_time()` None dönen uçuşlar (hiç zaman bilgisi yok) hiçbir
    bucket'a girmez - `flights_in_window`'ın "moment is None -> atla"
    davranışıyla AYNI.

    İPTAL/YÖNLENDİRİLMİŞ uçuşlar da dahil TÜM uçuşlar bucket'a girer -
    erken filtrelenmez; include_excluded=True/False ayrımı OKUMA anında
    (bkz. predict_airport) uygulanır - `flights_in_window`'ın mevcut
    davranışıyla AYNI.

    Flight OBJELERİ kopyalanmaz - aynı referanslar sadece farklı
    listelere eklenir (ek bellek maliyeti sadece dict/list iskeleti
    kadardır, uçuş verisi tekrar üretilmez).

    Uçuşların process_flights içindeki SIRASI korunur (tek geçişte,
    sırayla ekleniyor) - bu, `detect_diversions`/`detect_aircraft_changes`
    gibi liste sırasına bağlı çıktıların (ör. reasons listesi sırası)
    eski davranışla BİREBİR aynı kalmasını garantiler.
    """
    buckets: dict[datetime, list] = {}
    for flight in flights:
        moment = effective_time(flight)
        if moment is None:
            continue
        key = floor_to_window(moment, window_minutes)
        buckets.setdefault(key, []).append(flight)
    return buckets


def _predict_window_core(
    airport_iata: str,
    process: str,
    window_start: datetime,
    window_flights: list,
    window_all: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    historical_baseline: float | None,
    aircraft_match_rate: float,
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    historical_passenger_baseline: float | None = None,
) -> WindowPrediction:
    """
    `predict_window()`'ın ASIL hesap gövdesi - ADIM 5E-2'de buradan
    ayrıştırıldı. Farkı: pencereye giren uçuş listelerini (`window_flights`/
    `window_all`) KENDİSİ taramıyor, ÇAĞIRANDAN hazır alıyor. Skorlama/
    neden tespiti/confidence mantığının TEK VE AYNI kopyası - hem eski
    `predict_window()` (geriye dönük uyumluluk, doğrudan çağıranlar için)
    hem de `predict_airport()`'ın yeni bucket tabanlı hızlı yolu BU
    fonksiyonu çağırır; böylece iki yol arasında sonuç farkı OLAMAZ.
    """
    window_end = window_start + timedelta(minutes=window_minutes)

    if process == PROCESS_PASSPORT:
        score = passport_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes
        )
        rho = score["utilization"]
    else:
        score = security_density_score(
            window_flights,
            historical_baseline,
            demand.passenger_demand,
            historical_passenger_baseline,
        )
        # Security'de kapasite verisi yok; utilization da dakika da üretilmez.
        rho = None

    # MADDE 8: her aircraft-change event'i, FLIGHT'ın şu anki konumuna
    # göre değil, KENDİ flight_effective_time'ına göre bu pencereye
    # aitse dahil edilir. Bir uçuşun aynı pencerede birden fazla
    # değişimi varsa (A320->A321, sonra A321->A330) HER İKİSİ de aynı
    # anda burada kalabilir; window sınırı [window_start, window_end)
    # yarı-açık aralığıyla, sistemin geri kalanıyla AYNI kuralla
    # uygulanır (bkz. flights_in_window).
    window_changes: dict[str, list[tuple[str | None, str | None]]] | None = None
    if aircraft_changes:
        window_changes = {}
        for key, change_list in aircraft_changes.items():
            matching = [
                (old_icao, new_icao)
                for old_icao, new_icao, event_time in change_list
                if event_time is not None
                and window_start <= event_time < window_end
            ]
            if matching:
                window_changes[key] = matching

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
        flight_ratio=score.get("flight_ratio"),
        passenger_ratio=score.get("passenger_ratio"),
        reasons=reasons,
    )


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
    historical_passenger_baseline: float | None = None,
) -> WindowPrediction:
    """
    Tek pencere hesabı. Veritabanına dokunmaz. PUBLIC API - imza ve
    davranış ADIM 5E-2'de DEĞİŞMEDİ (doğrudan çağıranlar/testler için
    korundu).

    process_flights : bu havalimanının, bu süreci besleyen uçuşları
                      (AŞAMA 2 filtresinden geçmiş)
    historical_passenger_baseline
                    : MADDE 7 - security'nin yolcu oranı için geçmiş
                      ortalama yolcu talebi. Yoksa None; passenger_ratio
                      hesaplanmaz, uydurulmaz.

    NOT: Bu fonksiyon HÂLÂ `flights_in_window()` ile process_flights'ın
    TAMAMINI tarar (eski O(N) davranış) - `run_predictions()`'ın gerçek
    production yolu (bkz. `predict_airport`) artık bunun yerine
    `_bucket_flights_by_window()` + `_predict_window_core()` kullanıyor
    (O(N×W) yerine O(N)). İkisi de AYNI `_predict_window_core()`'u
    çağırdığı için sonuçlar birebir aynıdır - bu fonksiyon sadece
    tek-pencere senaryoları/testler için geriye dönük uyumluluk amacıyla
    tutuluyor.
    """
    window_flights = flights_in_window(
        process_flights, window_start, window_minutes
    )
    window_all = flights_in_window(
        process_flights, window_start, window_minutes, include_excluded=True
    )
    return _predict_window_core(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        window_flights=window_flights,
        window_all=window_all,
        config=config,
        demand=demand,
        historical_baseline=historical_baseline,
        aircraft_match_rate=aircraft_match_rate,
        aircraft_changes=aircraft_changes,
        window_minutes=window_minutes,
        historical_passenger_baseline=historical_passenger_baseline,
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
    passenger_baseline_fn=None,
) -> list[WindowPrediction]:
    """
    Bir havalimanının iki süreci için tüm pencereler.

    baseline_fn : (process, window_start) -> float | None
                  Geçmiş ortalama UÇUŞ sayısı. Geçmiş veri yoksa None
                  döndürmeli; SAHTE BASELINE ÜRETİLMEZ.
    passenger_baseline_fn
                : (process, window_start) -> float | None
                  MADDE 7 - geçmiş ortalama YOLCU talebi. Ayrı bir
                  fonksiyondur çünkü yolcu örneklemi uçuş örnekleminden
                  bağımsız birikir; biri varken diğeri henüz olmayabilir.

    ADIM 5E-2 - performans: process başına uçuşlar TEK bir geçişte
    (`_bucket_flights_by_window`) 15 dk pencerelere önceden gruplanır -
    `effective_time()` uçuş başına TAM OLARAK BİR KEZ hesaplanır (eskiden
    pencere sayısı kadar tekrar tekrar hesaplanıyordu). Matematiksel
    sonuç `predict_window()`'ın eski O(N×W) taramasıyla BİREBİR AYNIDIR
    (bkz. `_bucket_flights_by_window` docstring'indeki eşdeğerlik kanıtı)
    - ikisi de aynı `_predict_window_core()`'u çağırır.
    """
    if aircraft_match_rate is None:
        aircraft_match_rate = flight_match_rate(flights)

    predictions: list[WindowPrediction] = []

    for process in PROCESSES:
        relevant = _PROCESS_FLIGHTS[process](flights)
        buckets = _bucket_flights_by_window(relevant, window_minutes)

        for start in sorted(buckets):
            window_all = buckets[start]
            # Talep hesabına giren uçuşlar (iptal/diverted hariç) -
            # `flights_in_window(..., include_excluded=False)` ile AYNI
            # filtre, ama artık N değil sadece bu pencerenin (~N/W
            # büyüklüğündeki) uçuşları üzerinde.
            window_flights = [
                f for f in window_all if f.status not in EXCLUDED_STATUSES
            ]

            baseline = baseline_fn(process, start) if baseline_fn else None
            passenger_baseline = (
                passenger_baseline_fn(process, start)
                if passenger_baseline_fn else None
            )
            predictions.append(_predict_window_core(
                airport_iata=airport_iata,
                process=process,
                window_start=start,
                window_flights=window_flights,
                window_all=window_all,
                config=config,
                demand=demand,
                historical_baseline=baseline,
                aircraft_match_rate=aircraft_match_rate,
                aircraft_changes=aircraft_changes,
                window_minutes=window_minutes,
                historical_passenger_baseline=passenger_baseline,
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
    """Uçuş baseline okumasını (süreç, pencere) çiftine bağlar."""
    def lookup(process: str, window_start: datetime) -> float | None:
        return get_baseline(
            session,
            airport_iata=airport_iata,
            process=process,
            hour_of_day=window_start.hour,
            day_of_week=window_start.weekday(),
        )
    return lookup


def _db_passenger_baseline_fn(session, airport_iata: str):
    """MADDE 7 - yolcu baseline okumasını (süreç, pencere) çiftine bağlar."""
    def lookup(process: str, window_start: datetime) -> float | None:
        return get_passenger_baseline(
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
        existing.flight_ratio = prediction.flight_ratio
        existing.passenger_ratio = prediction.passenger_ratio
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
    session, predictions: list[WindowPrediction], now: datetime | None = None
) -> int:
    """
    Pencere uçuş sayılarını clustering baseline'ına ekler.

    ÖNEMLİ: Tahminler hesaplandıktan SONRA çağrılır; yoksa pencere
    kendi baseline'ını beslemiş olur. Baseline birimi bu yüzden
    "o saat/gün için bir 15 dk penceredeki ortalama uçuş sayısı"dır -
    security ratio'su da aynı birimle karşılaştırılır.

    MADDE 3: SADECE kapanmış pencereler (window_end <= now) baseline'a
    girer. Açık pencere - o an hâlâ uçuş kazanıp kaybedebilir, nihai
    flight_count'u henüz belli değildir - baseline'a hiç yazılmaz.
    Kapanmış bir pencere tekrar tekrar refresh edilse bile
    record_observation() idempotent olduğu için havuz bir daha
    güncellenmez (bkz. baseline.py).

    `now` test edilebilirlik için enjekte edilebilir; verilmezse
    domain_now() (gerçek saat) kullanılır.

    ADIM 5E-5 - performans (bkz. ADIM 5E-3/5E-4 analizi): bu döngü
    boyunca session'ın `expire_on_commit`'i GEÇİCİ olarak `False`'a
    çekilir - `app/db.py`'deki global `sessionmaker` ayarına
    DOKUNULMAZ, sadece bu fonksiyonun ömrü boyunca, `finally` ile
    garanti altında geri yüklenir. Gerekçe: `record_observation()`'ın
    pencere başına yaptığı commit'ler (bkz. baseline.py), session'da bu
    noktada hâlâ yüklü olan - ve bu fonksiyon çalıştığı sürece bir daha
    HİÇ okunmayan - uçuş ORM nesnelerini SQLAlchemy'nin varsayılan
    `expire_on_commit=True` davranışıyla gereksiz yere expire ediyordu
    (N=10.000 flight'ta ölçülen maliyetin ~%90'ı burasıydı). Commit
    sayısı, IntegrityError/rollback deseni, unique constraint ve
    idempotency mantığının HİÇBİRİ değişmedi - sadece artık kullanılmayan
    obje cache'inin boşa invalidation'ı kalkıyor.
    """
    now = now if now is not None else domain_now()
    recorded = 0

    old_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        for prediction in predictions:
            if now < prediction.window_end:
                continue  # açık pencere - baseline'a yazma
            record_observation(
                session,
                airport_iata=prediction.airport_iata,
                process=prediction.process,
                window_start=prediction.window_start,
                hour_of_day=prediction.window_start.hour,
                day_of_week=prediction.window_start.weekday(),
                flight_count=prediction.flight_count,
                expected_passengers=prediction.expected_passengers,
            )
            recorded += 1
    finally:
        session.expire_on_commit = old_expire_on_commit

    return recorded


def run_predictions(
    session,
    resolver,
    airports: list[str] | None = None,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    update_baseline: bool = True,
    now: datetime | None = None,
) -> dict:
    """
    Sistemdeki HER havalimanı için tahminleri üretir ve kaydeder.

    resolver : Madde 1'in AircraftCapacityService örneği. Bu servis
               burada DEĞİŞTİRİLMEZ, sadece kullanılır.
    now      : MADDE 3 - açık/kapalı pencere kontrolü için "şu an".
               Test edilebilirlik için enjekte edilebilir; verilmezse
               domain_now() (gerçek saat) kullanılır.

    Hata izolasyonu (production hardening): her havalimanı KENDİ
    try/except bloğunda hesaplanır. Bir havalimanının tahmini
    üretilirken hata oluşursa (bozuk config, beklenmeyen uçuş verisi
    vb.) SADECE o havalimanı atlanır - session.rollback() ile o
    havalimanının yarım kalan işlemi geri alınır, hata loglanır,
    diğer havalimanlarının hesabı ETKİLENMEDEN devam eder. Başarısız
    havalimanları `failed_airports` listesinde döner; mevcut dönüş
    sözleşmesindeki hiçbir alan kaldırılmadı/yeniden adlandırılmadı.
    """
    from .ingestion.refresh import aircraft_changes_for_airport

    codes = airports if airports is not None else airport_codes(session)
    configs = get_configs(session, codes)

    total: list[WindowPrediction] = []
    per_airport: dict[str, int] = {}
    failed_airports: list[str] = []
    pruned = 0

    for code in codes:
        try:
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
                passenger_baseline_fn=_db_passenger_baseline_fn(session, code),
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
        except Exception:
            # Bilinçli geniş except: havalimanı-bazlı izolasyon sınırı
            # (bkz. ingestion/refresh.py aynı desen). Hata türü önceden
            # bilinemez; loglanır + sayılır, sessizce yutulmaz.
            session.rollback()
            failed_airports.append(code)
            logger.exception(
                "Havalimanı için tahmin üretilemedi, atlanıyor (airport=%s)",
                code,
            )
            continue

    written = persist_predictions(session, total)

    if update_baseline:
        record_baseline_observations(session, total, now=now)

    return {
        "airports": per_airport,
        "predictions": len(total),
        "pruned": pruned,
        "failed_airports": failed_airports,
        **written,
    }
