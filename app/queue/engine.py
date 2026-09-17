"""
AŞAMA 5 + 6 + 7 + 8 - Tahmin motoru (orkestrasyon).

Saf katmanları (scoring, detector, demand, flows) tek bir akışta
birleştirir:

    uçuş listesi
      -> süreç filtresi (security / passport)
      -> 15 dk pencereler
      -> skor (security + passport: ortak Erlang-C/backlog çekirdeği)
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

from dataclasses import dataclass, field, replace
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
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    REASON_CAPACITY_EXCEEDED,
    REASON_NO_BASELINE,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
)
from .core.event_queue import simulate_passport, simulate_security
from .domain.operational_day import (
    filter_flights_for_operational_day,
    operational_date,
    resolve_airport_timezone,
)
from .core.scoring import (
    confidence_score,
    domestic_security_capacity_rate,
    international_security_capacity_rate,
    passport_capacity_rate,
    passport_effective_server_count,
    passport_queue_model,
    security_capacity_rate,
    security_density_score,
    security_queue_model,
)
from .domain.demand import DemandCalculator, effective_time, flights_in_window
from .domain.flows import (
    passport_arrival_flights,
    passport_departure_flights,
    passport_flights,
    security_domestic_flights,
    security_flights,
    security_international_flights,
)
from .models import Airport, Flight, QueuePrediction
from .reasons.detector import DetectedReason, detect_reasons

logger = logging.getLogger(__name__)

# ADIM (Security Domestic/International Split) - birleşik PROCESS_SECURITY
# (TÜM kalkışlar) GERİYE DÖNÜK UYUMLULUK için AYNEN üretilmeye devam
# ediyor (silinmedi); PROCESS_SECURITY_DOMESTIC/PROCESS_SECURITY_INTL bunun
# yanında EK olarak üretilir. Her üç security süreci AYNI ortak queue
# çekirdeğini kendi flight alt kümesi üzerinde çağırır; tarihsel density
# oranları yalnız açıklayıcı/baseline metrik olarak korunur.
PROCESSES = (
    PROCESS_SECURITY, PROCESS_PASSPORT,
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
)

# Hangi sürecin hangi uçuşlarla beslendiği (AŞAMA 2).
_PROCESS_FLIGHTS = {
    PROCESS_SECURITY: security_flights,
    PROCESS_PASSPORT: passport_flights,
    PROCESS_SECURITY_DOMESTIC: security_domestic_flights,
    PROCESS_SECURITY_INTL: security_international_flights,
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
    # ortalamasıdır. Queue risk/wait hesabından bağımsız tanı metrikleridir.
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
    backlog_start: float = 0.0,
    now: datetime | None = None,
    demand_override: float | None = None,
    current_arrived_override: float | None = None,
    lane_count_override: int | None = None,
) -> WindowPrediction:
    """
    `predict_window()`'ın ASIL hesap gövdesi - ADIM 5E-2'de buradan
    ayrıştırıldı. Farkı: pencereye giren uçuş listelerini (`window_flights`/
    `window_all`) KENDİSİ taramıyor, ÇAĞIRANDAN hazır alıyor. Skorlama/
    neden tespiti/confidence mantığının TEK VE AYNI kopyası - hem eski
    `predict_window()` (geriye dönük uyumluluk, doğrudan çağıranlar için)
    hem de `predict_airport()`'ın yeni bucket tabanlı hızlı yolu BU
    fonksiyonu çağırır; böylece iki yol arasında sonuç farkı OLAMAZ.

    now : ADIM 6D-2 - queue "an itibariyle" (current) bekleme hesabı
          için "şu an". Verilmezse `domain_now()` (gerçek saat)
          kullanılır - `run_predictions()`'ın `now` parametresiyle AYNI
          desen. Passport ve security aynı current-arrived semantiğini
          kullanır.
    demand_override / current_arrived_override
        : PASSPORT→SECURITY zaman-kuplajı için (bkz. `predict_airport`,
          `_passport_security_hourly_coupling`) - SADECE
          PROCESS_SECURITY/PROCESS_SECURITY_INTL için, `window_flights`'ın
          KENDİ toplamı yerine kuplaj hesabından gelen GERÇEK
          (zaman-kaydırmalı) talep kullanılır. `window_flights` bu
          durumda yine de `flight_count`/neden tespiti için okunur.
          Verilmezse (None, passport ve security_domestic için hep
          None) eski davranış birebir korunur.
    lane_count_override
        : ADIM (Domestic/International Security Lane Ayrımı) - SADECE
          `PROCESS_SECURITY_DOMESTIC` için, `config.security_lane_count`
          yerine `config.domestic_security_lane_count` kullanılmasını
          sağlar (bkz. `predict_airport`). Verilmezse (None, diğer tüm
          süreçlerde hep None) eski davranış birebir korunur.
    """
    window_end = window_start + timedelta(minutes=window_minutes)

    _now = now if now is not None else domain_now()
    elapsed_minutes = max(0.0, min(
        (_now - window_start).total_seconds() / 60.0, window_minutes
    ))
    if current_arrived_override is not None:
        current_arrived_demand = current_arrived_override
    else:
        # ADIM 6D-2 §D - SADECE effective_time(f) <= now olan uçuşlar
        # "an itibariyle" kuyruğa girer. Henüz gelmemiş uçuşlar current
        # wait'e girmez; tam pencere demand/backlog recurrence'ına girer.
        current_arrived_demand = sum(
            demand.passenger_demand(f) for f in window_flights
            if (t := effective_time(f)) is not None and t <= _now
        )

    if process == PROCESS_PASSPORT:
        score = passport_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes,
            backlog_start=backlog_start,
            current_arrived_demand=current_arrived_demand,
            elapsed_minutes=elapsed_minutes,
        )
    else:
        score = security_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes,
            backlog_start=backlog_start,
            current_arrived_demand=current_arrived_demand,
            elapsed_minutes=elapsed_minutes,
            demand_override=demand_override,
            lane_count_override=lane_count_override,
        )
        density = security_density_score(
            window_flights,
            historical_baseline,
            demand.passenger_demand,
            historical_passenger_baseline,
        )
        # Tarihsel karşılaştırmalar queue riskinin yerine geçmez; yalnız
        # mevcut API alanlarında açıklayıcı metrik olarak korunur.
        score["baseline_ratio"] = density.get("baseline_ratio")
        score["flight_ratio"] = density.get("flight_ratio")
        score["passenger_ratio"] = density.get("passenger_ratio")
        score["reasons"] = score.get("reasons", []) + density.get("reasons", [])

    rho = score["utilization"]

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
    backlog_start: float = 0.0,
    now: datetime | None = None,
) -> WindowPrediction:
    """
    Tek pencere hesabı. Veritabanına dokunmaz. PUBLIC API - imza ve
    davranış ADIM 5E-2'de DEĞİŞMEDİ (doğrudan çağıranlar/testler için
    korundu); ADIM 6D SADECE opsiyonel `backlog_start` parametresini
    EKLEDİ (varsayılan 0.0 - vermeyen eski çağıranlar ESKİ davranışla
    birebir aynı sonucu alır, bkz. passport_queue_model docstring'i);
    ADIM 6D-2 SADECE opsiyonel `now` parametresini EKLEDİ (verilmezse
    `domain_now()`).

    process_flights : bu havalimanının, bu süreci besleyen uçuşları
                      (AŞAMA 2 filtresinden geçmiş)
    historical_passenger_baseline
                    : MADDE 7 - security'nin yolcu oranı için geçmiş
                      ortalama yolcu talebi. Yoksa None; passenger_ratio
                      hesaplanmaz, uydurulmaz.
    backlog_start   : passport ve security için önceki saatten devreden
                      kuyruk. Bu fonksiyon TEK bir pencereyi
                      hesapladığı için önceki pencereden gelen backlog'u
                      KENDİSİ HESAPLAMAZ - çağıran taraf (kronolojik
                      zincir `predict_airport`'ta) besler.
    now             : ADIM 6D-2 - passport/security "an itibariyle"
                      (current) bekleme hesabı için "şu an".

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
        backlog_start=backlog_start,
        now=now,
    )


def _queue_backlog_starts(
    buckets: dict[datetime, list],
    capacity_rate: float,
    demand_fn,
    window_minutes: int,
) -> dict[datetime, float]:
    """
    Passport/security için, bu havalimanı+sürecin KENDİ pencereleri
    üzerinde KRONOLOJİK backlog zinciri.

    `buckets` sadece uçuşu OLAN pencereleri içerir (bkz. `window_starts`
    docstring'i - boş pencereye satır açılmaz). Ama backlog, gişelerin
    o boş pencerelerde de yolcu işlemeye DEVAM ettiği gerçeğini
    yansıtmalı - aksi halde iki dolu pencere arasında (mesela) 45 dk'lık
    sakin bir ara varsa, o 45 dk boyunca hiç servis olmamış gibi
    davranıp backlog'u YAPAY OLARAK ŞİŞİRİRDİK. Bu yüzden zincir,
    ilk ve son dolu pencere arasındaki HER 15 dk'lık pencereyi (boş
    olanlar dahil, `window_minutes` adımlarla) sırayla dolaşır; sadece
    dolu pencereler için `backlog_start` DÖNDÜRÜLÜR, ama boş
    pencerelerin de servis kapasitesi backlog'dan düşülür.

    Recurrence (ortak queue modelindeki AYNI formül):

        service_capacity = (c * mu) * window_minutes    (kişi)
        backlog_end = max(0, backlog_start + demand - service_capacity)

    Tamamen bu havalimanının kendi `buckets`'ından türetildiği ve hiçbir
    DB'den önceki çalışmanın backlog'unu OKUMADIĞI için, aynı `flights`
    ile tekrar çağrıldığında HER ZAMAN aynı sonucu üretir (idempotent).
    """
    starts = sorted(buckets)
    if not starts:
        return {}

    service_capacity = capacity_rate * window_minutes

    backlog_starts: dict[datetime, float] = {}
    backlog = 0.0
    current = starts[0]
    last = starts[-1]
    step = timedelta(minutes=window_minutes)

    while current <= last:
        window_all = buckets.get(current, [])
        window_flights = [
            f for f in window_all if f.status not in EXCLUDED_STATUSES
        ]
        demand = sum(demand_fn(f) for f in window_flights)

        if current in buckets:
            backlog_starts[current] = backlog

        backlog = max(0.0, backlog + demand - service_capacity)

        current += step

    return backlog_starts


def _hourly_backlog_chain(
    demand_by_hour: dict[datetime, float],
    capacity_rate: float,
    window_minutes: int,
) -> dict[datetime, float]:
    """
    `_queue_backlog_starts()` ile BİREBİR AYNI recurrence
    (`backlog_end = max(0, backlog_start + demand - service_capacity)`),
    ama Flight nesneleri yerine ÖNCEDEN hesaplanmış saatlik
    `demand_by_hour` sözlüğü üzerinde çalışır - `security`/`security_intl`
    için passport'tan zaman-kaydırmalı devralınan talep artık gerçek
    Flight listesi değil, düz bir sayı (bkz.
    `_passport_security_hourly_coupling`). Formülün KENDİSİ hiç
    değişmedi, sadece girdi kaynağı farklı.

    İlk/son dolu saat arasındaki BOŞ saatler de dolaşılır (gişeler o
    saatlerde de servise devam eder) - `_queue_backlog_starts` ile AYNI
    ilke.
    """
    starts = sorted(demand_by_hour)
    if not starts:
        return {}

    service_capacity = capacity_rate * window_minutes
    backlog_start: dict[datetime, float] = {}

    backlog = 0.0
    current = starts[0]
    last = starts[-1]
    step = timedelta(minutes=window_minutes)

    while current <= last:
        demand_here = demand_by_hour.get(current, 0.0)
        backlog_start[current] = backlog
        backlog = max(0.0, backlog + demand_here - service_capacity)
        current += step

    return backlog_start


def _event_driven_queue_demand(
    flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    now: datetime | None = None,
) -> dict:
    """
    ADIM (Event-Driven Engine Entegrasyonu) - `core/event_queue.py`'nin
    gerçek discrete-event simülasyonundan türetilen saatlik demand/
    backlog/current-released sözlükleri.

    Bu fonksiyon, eski `_passport_security_hourly_coupling()`'in YERİNİ
    ALIR (AYNI dönen sözlük şekli: `demand_by_hour[process][hour]`,
    `backlog_start_by_hour[process][hour]`, `current_released_by_hour
    [process][hour]` - `predict_airport()`'un çağrı sözleşmesi DEĞİŞMEDİ,
    böylece WindowPrediction/QueuePrediction/API şeması bu ADIM'da
    DEĞİŞMEDEN kalır) - ama ARTIK saatlik-oransal bir YAKLAŞIKLIK
    DEĞİL, `simulate_passport()`/`simulate_security()` (heapq tabanlı,
    gerçek arrival/service-start/completion zaman damgalı FIFO
    simülasyon) üzerinden hesaplanan GERÇEK sonuçlardır.

    BÖLÜM 14 (grafik zamanı ≠ queue zamanı): `simulate_*` fonksiyonları
    İÇERİDE tam dakika/saniye hassasiyetiyle çalışır (`ServiceEvent.
    arrival_time`/`service_start_time`/`completion_time`); bu fonksiyon
    SADECE dışarı aktarırken (`floor_to_window`) saatlik bucket'a
    yuvarlar - internal simülasyonun kendisi HİÇBİR ZAMAN saatlik
    adımlarla ilerlemedi.

    BÖLÜM 17 (paylaşılan passport havuzu double-count edilmez):
    `simulate_passport()` departure+arrival için TEK bir çağrı, TEK bir
    `effective_server_count`'luk heap kullanır (bkz. event_queue.py) -
    departure'a ayrı 8, arrival'a ayrı 8 server VERİLMEZ. PROCESS_PASSPORT
    departure+arrival'ın TOPLAMINI raporlar (mevcut davranış, DEĞİŞMEDİ).

    BÖLÜM 35 (overall/process ayrımı): bu fonksiyon YENİ bir fiziksel
    queue YARATMAZ - sadece MEVCUT 4 sürecin (PASSPORT, SECURITY_DOMESTIC,
    SECURITY birleşik, SECURITY_INTL) HER BİRİ kendi BAĞIMSIZ simülasyonu
    (kendi heap'i, kendi sunucu sayısı) ile hesaplanır; hiçbiri diğerinin
    sunucularını PAYLAŞMAZ (PASSPORT'un paylaştığı TEK istisna departure/
    arrival ayrımıdır, security süreçleriyle DEĞİL).

    PROCESS_SECURITY (birleşik, legacy): önceki coupling'in belgelenmiş
    semantiğiyle TUTARLI - domestic-direct + passport'tan serbest
    bırakılan international talebi, KENDİ BAĞIMSIZ (`security_lane_count`
    kapasiteli) simülasyonunda birleştirir; `PROCESS_SECURITY_DOMESTIC`/
    `PROCESS_SECURITY_INTL`'in fiziksel lane'lerini BİR DAHA SAYMAZ - bu
    ÜÇ security süreci üç AYRI `simulate_security()` çağrısıdır, sunucu
    HEAP'i PAYLAŞMAZLAR (fiziksel çakışma/double-count yok).

    `PROCESS_SECURITY_INTL` artık GERÇEKTEN `config.
    international_security_lane_count`'u kullanır (önceki ADIM'da sadece
    config şemasında hazırdı, hesaba BAĞLANMAMIŞTI - bu ADIM'da bağlandı).

    24 SAAT UFKU KALDIRILDI: eski coupling'in `horizon = last +
    24*window` sınırı (bilinçli, belgelenmiş bir sınırlamaydı) burada YOK
    - `simulate_*` fonksiyonları backlog TAMAMEN boşalana kadar çalışır
    (persisted/24h state'e GEÇİLMEDİ - bu ADIM'ın kapsamı dışında, ama
    tek bir `predict_airport()` çağrısı için artık passenger KAYBI YOK).

    now : "an itibariyle" (current) kısmi serbest bırakma toplamı için -
          `_predict_window_core`'un `current_arrived_demand` mantığıyla
          AYNI "sadece şimdiye kadar gerçekleşen" ilkesi. Artık GERÇEK
          `ServiceEvent.arrival_time <= now` kontrolüyle KESİN hesaplanır
          (bkz. `_bucket_current_released`) - eski elapsed-ORANI
          YAKLAŞIKLIĞI (saat içi tekdüze varış varsayımı) gerekmiyor,
          çünkü artık her sürecin GERÇEK event zaman damgası elimizde.
    """
    from .domain.flows import is_international_arrival, is_international_departure

    _now = now if now is not None else domain_now()

    def _arrivals(flight_list, predicate=None) -> list[tuple[datetime, float]]:
        result = []
        for f in flight_list:
            if f.status in EXCLUDED_STATUSES:
                continue
            if predicate is not None and not predicate(f):
                continue
            moment = effective_time(f)
            if moment is None:
                continue
            result.append((moment, demand.passenger_demand(f)))
        return result

    departure_arrivals = _arrivals(passport_flights(flights), is_international_departure)
    arrival_arrivals = _arrivals(passport_flights(flights), is_international_arrival)
    domestic_arrivals = _arrivals(security_domestic_flights(flights))

    passport_result = simulate_passport(
        departure_arrivals,
        arrival_arrivals,
        passport_effective_server_count(config),
        config.passport_service_time_minutes,
    )

    # AŞAMA 9 - passport'un GERÇEK completion timestamp'i security'nin
    # arrival timestamp'i olur (saatlik-oransal tahmin DEĞİL).
    security_intl_arrivals = [
        (event.completion_time, event.count) for event in passport_result["departure"]
    ]
    security_intl_events = simulate_security(
        security_intl_arrivals,
        config.international_security_lane_count,
        config.security_service_time_minutes,
        origin=PROCESS_SECURITY_INTL,
    )

    security_dom_events = simulate_security(
        domestic_arrivals,
        config.domestic_security_lane_count,
        config.security_service_time_minutes,
        origin=PROCESS_SECURITY_DOMESTIC,
    )

    # PROCESS_SECURITY (birleşik, legacy) - AYRI/kendi kapasiteli simülasyon
    # (yukarıdaki docstring'de açıklanan, önceden belgelenmiş semantik).
    security_combined_events = simulate_security(
        domestic_arrivals + security_intl_arrivals,
        config.security_lane_count,
        config.security_service_time_minutes,
        origin=PROCESS_SECURITY,
    )

    def _bucket_by_arrival(events) -> dict[datetime, float]:
        buckets: dict[datetime, float] = {}
        for event in events:
            key = floor_to_window(event.arrival_time, window_minutes)
            buckets[key] = buckets.get(key, 0.0) + event.count
        return buckets

    def _bucket_current_released(events) -> dict[datetime, float]:
        """
        "An itibariyle" (current) - GERÇEK event.arrival_time <= now olan
        birimlerin TOPLAMI, saate bucket'lanmış.

        Eski `_passport_security_hourly_coupling()` bunu (SADECE
        PROCESS_SECURITY/PROCESS_SECURITY_INTL için) "açık pencerenin
        elapsed-ORANI" ile YAKLAŞIK hesaplıyordu (saat içi tekdüze varış
        varsayımı). Artık DÖRT sürecin de gerçek `ServiceEvent.arrival_
        time`'ı elimizde - bu yüzden YAKLAŞIK orana gerek YOK, `_predict_
        window_core`'un eski PASSPORT/SECURITY_DOMESTIC yolunun zaten
        yaptığı TAM "effective_time(f) <= now" kontrolüyle BİREBİR AYNI
        (ve PROCESS_SECURITY/PROCESS_SECURITY_INTL için de artık aynı
        kesinlikte) kesin toplam üretir.
        """
        buckets: dict[datetime, float] = {}
        for event in events:
            if event.arrival_time > _now:
                continue
            key = floor_to_window(event.arrival_time, window_minutes)
            buckets[key] = buckets.get(key, 0.0) + event.count
        return buckets

    process_events = {
        PROCESS_PASSPORT: passport_result["departure"] + passport_result["arrival"],
        PROCESS_SECURITY_DOMESTIC: security_dom_events,
        PROCESS_SECURITY: security_combined_events,
        PROCESS_SECURITY_INTL: security_intl_events,
    }

    demand_by_hour = {
        process: _bucket_by_arrival(events)
        for process, events in process_events.items()
    }

    capacity_rates = {
        PROCESS_PASSPORT: passport_capacity_rate(config),
        PROCESS_SECURITY_DOMESTIC: domestic_security_capacity_rate(config),
        PROCESS_SECURITY: security_capacity_rate(config),
        PROCESS_SECURITY_INTL: international_security_capacity_rate(config),
    }

    backlog_start_by_hour = {
        process: _hourly_backlog_chain(
            demand_by_hour[process], capacity_rates[process], window_minutes
        )
        for process in demand_by_hour
    }

    current_released_by_hour = {
        process: _bucket_current_released(events)
        for process, events in process_events.items()
    }

    return {
        "demand_by_hour": demand_by_hour,
        "backlog_start_by_hour": backlog_start_by_hour,
        "current_released_by_hour": current_released_by_hour,
    }


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
    now: datetime | None = None,
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
    now         : ADIM 6D-2 - queue "an itibariyle" (current) bekleme
                  hesabı için "şu an" (bkz. `_predict_window_core`).
                  Verilmezse `domain_now()`.

    ADIM 5E-2 - performans: process başına uçuşlar TEK bir geçişte
    (`_bucket_flights_by_window`) 15 dk pencerelere önceden gruplanır -
    `effective_time()` uçuş başına TAM OLARAK BİR KEZ hesaplanır (eskiden
    pencere sayısı kadar tekrar tekrar hesaplanıyordu). Matematiksel
    sonuç `predict_window()`'ın eski O(N×W) taramasıyla BİREBİR AYNIDIR
    (bkz. `_bucket_flights_by_window` docstring'indeki eşdeğerlik kanıtı)
    - ikisi de aynı `_predict_window_core()`'u çağırır.
    """
    # Invalid config gerçek overload gibi persist edilmez. Ortak helper'lar
    # server count ve service time'ın pozitif olduğunu açıkça doğrular.
    # Dördü de burada erken doğrulanır (BUG-03 davranışı korunuyor) - hiçbiri
    # aşağıdaki event-driven simülasyon içinde SESSİZCE patlamaz.
    try:
        passport_capacity_rate(config)
        security_capacity_rate(config)
        domestic_security_capacity_rate(config)
        international_security_capacity_rate(config)
    except ValueError as exc:
        raise ValueError(f"{airport_iata}: geçersiz queue config - {exc}") from exc

    if aircraft_match_rate is None:
        aircraft_match_rate = flight_match_rate(flights)

    predictions: list[WindowPrediction] = []

    # ADIM (Event-Driven Engine Entegrasyonu) - PASSPORT, SECURITY_DOMESTIC,
    # SECURITY (birleşik) ve SECURITY_INTL'in DÖRDÜ DE artık
    # `_event_driven_queue_demand()`'ın gerçek discrete-event simülasyon
    # sonuçlarından beslenir (bkz. fonksiyon docstring'i - eski saatlik-
    # oransal `_passport_security_hourly_coupling()`'in YERİNE geçti).
    # `demand_override`/`backlog_start`/`current_arrived_override`
    # mekanizması DEĞİŞMEDİ (ADIM 6D'den beri var) - sadece bu değerlerin
    # KAYNAĞI artık event_queue.py.
    coupling = _event_driven_queue_demand(
        flights, config, demand, window_minutes=window_minutes, now=now
    )
    lane_count_overrides = {
        PROCESS_SECURITY_DOMESTIC: config.domestic_security_lane_count,
        PROCESS_SECURITY_INTL: config.international_security_lane_count,
    }

    for process in PROCESSES:
        relevant = _PROCESS_FLIGHTS[process](flights)
        buckets = _bucket_flights_by_window(relevant, window_minutes)

        coupled_demand = coupling["demand_by_hour"][process]
        coupled_backlog = coupling["backlog_start_by_hour"][process]
        coupled_current = coupling["current_released_by_hour"][process]
        # starts = RAW uçuş pencereleri (flight_count/neden tespiti için,
        # DEĞİŞMEDİ) BİRLEŞİMİ simülasyonun ürettiği GERÇEK saatlerle -
        # ikisi FARKLI olabilir (ör. passport geç serbest bıraktığı için
        # security'ye ancak bir SONRAKİ saatte ulaşan yolcular; PASSPORT/
        # SECURITY_DOMESTIC için ikisi PRATİKTE AYNIDIR çünkü arrival_time
        # zaten flight'ın kendi effective_time'ıdır).
        starts = sorted(set(buckets) | set(coupled_demand))
        for start in starts:
            window_all = buckets.get(start, [])
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
                backlog_start=coupled_backlog.get(start, 0.0),
                aircraft_changes=aircraft_changes,
                window_minutes=window_minutes,
                historical_passenger_baseline=passenger_baseline,
                now=now,
                demand_override=coupled_demand.get(start, 0.0),
                current_arrived_override=coupled_current.get(start, 0.0),
                lane_count_override=lane_count_overrides.get(process),
            ))

    predictions.extend(_passport_cohort_breakdown(predictions, flights, demand))

    return predictions


def _passport_cohort_breakdown(
    predictions: list[WindowPrediction], flights: list, demand: DemandCalculator,
) -> list[WindowPrediction]:
    """
    ADIM (4-Graph API Contract) - Bölüm 17: paylaşılan FİZİKSEL passport
    havuzunu (aynı 4x2=8 efektif server) İKİYE AYIRMADAN/duplicate
    ETMEDEN, International Departure ve International Arrival
    grafiklerinin ayrı ayrı okuyabileceği cohort/kaynak bazlı raporlama
    satırları üretir.

    KENDİ Erlang-C/backlog hesabı YOK - her birleşik `PROCESS_PASSPORT`
    penceresinin utilization/estimated_wait_minutes/risk/confidence/
    baseline alanları AYNEN kopyalanır (fiziksel kuyruk TEKTİR, iki
    "görünümü" de AYNI gerçek durumu yansıtmalı - biri diğerinden düşük
    kapasiteyle hesaplanmış SAHTE bir wait üretmez). SADECE
    `expected_passengers`/`flight_count` kendi cohort'una (departure-
    kökenli/arrival-kökenli) göre, `passport_flights()`'ın zaten var
    olan alt-kümeleriyle (RAW flight bucket, event simülasyonu ile AYNI
    saatlere düşer çünkü ikisi de aynı `effective_time()`'ı kullanır)
    yeniden bölünür.
    """
    departure_buckets = _bucket_flights_by_window(passport_departure_flights(flights))
    arrival_buckets = _bucket_flights_by_window(passport_arrival_flights(flights))

    def _cohort_totals(buckets, window_start):
        window_all = buckets.get(window_start, [])
        window_flights = [f for f in window_all if f.status not in EXCLUDED_STATUSES]
        return len(window_flights), sum(demand.passenger_demand(f) for f in window_flights)

    derived: list[WindowPrediction] = []
    for p in predictions:
        if p.process != PROCESS_PASSPORT:
            continue
        # Diğer split süreçlerle (SECURITY_DOMESTIC/SECURITY_INTL) TUTARLI:
        # bir cohort'un o pencerede HİÇ flight'ı yoksa satır ÜRETİLMEZ
        # (sahte sıfır-yolcu satırı yerine, mevcut "boş pencereye satır
        # açılmaz" ilkesi - bkz. `window_starts` docstring'i).
        if p.window_start in departure_buckets:
            dep_count, dep_demand = _cohort_totals(departure_buckets, p.window_start)
            derived.append(replace(
                p, process=PROCESS_PASSPORT_DEPARTURE,
                flight_count=dep_count, expected_passengers=dep_demand,
            ))
        if p.window_start in arrival_buckets:
            arr_count, arr_demand = _cohort_totals(arrival_buckets, p.window_start)
            derived.append(replace(
                p, process=PROCESS_PASSPORT_ARRIVAL,
                flight_count=arr_count, expected_passengers=arr_demand,
            ))
    return derived


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


def _airport_timezones(session, airport_codes) -> dict[str, str | None]:
    """{airport_iata: timezone_adı} - `Airport.timezone`'dan (gerçek
    kaynak: flight_airports.sql), UYDURULMAZ."""
    codes = list(airport_codes)
    if not codes:
        return {}
    rows = session.execute(
        select(Airport.iata_code, Airport.timezone).where(
            Airport.iata_code.in_(codes)
        )
    ).all()
    return dict(rows)


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

    # ADIM 6D-2: passport "an itibariyle" (current) bekleme hesabı
    # (predict_airport) VE açık/kapalı pencere kararı (record_baseline_
    # observations) AYNI "now" anını kullanmalı - burada BİR KEZ çözülüp
    # her ikisine de aynen geçilir.
    resolved_now = now if now is not None else domain_now()

    codes = airports if airports is not None else airport_codes(session)
    configs = get_configs(session, codes)
    timezones = _airport_timezones(session, codes)

    total: list[WindowPrediction] = []
    per_airport: dict[str, int] = {}
    failed_airports: list[str] = []
    # Bölüm 59 - güvenilir timezone kaynağı olmayan (alan boş VEYA
    # zoneinfo'da tanınmıyor) havalimanları için filtre UYGULANMAZ
    # (eski, tüm-geçmiş davranış korunur) - UYDURMA bir varsayım
    # ÜRETİLMEZ, sadece AÇIKÇA raporlanır (bkz. rapor/README).
    timezone_missing_airports: list[str] = []
    pruned = 0

    for code in codes:
        try:
            all_flights = flights_of_airport(session, code)
            if not all_flights:
                per_airport[code] = 0
                continue

            # Bölüm 58/59/61 - SADECE bu havalimanının BUGÜNKÜ (yerel)
            # operasyonel gününe ait uçuşlar YENİ demand kaynağıdır.
            # Seçim flight'ın KENDİ referans zamanıyla yapılır -
            # `effective_time()`'ın -120dk/+15dk kaydırdığı kuyruk
            # event zamanı DEĞİL (Bölüm 61) - bu yüzden sınır-geçişli
            # event'ler (19 Eylül uçuşu -> 18 Eylül kuyruk anı gibi)
            # BU FİLTREDEN ETKİLENMEZ, `effective_time()` DEĞİŞMEDEN
            # kendi hesabını yapmaya devam eder.
            tz = resolve_airport_timezone(timezones.get(code))
            if tz is not None:
                flights = filter_flights_for_operational_day(
                    all_flights, tz, resolved_now
                )
                logger.info(
                    "operational-day filtresi uygulandı (airport=%s, "
                    "local_date=%s, %d/%d uçuş seçildi)",
                    code, operational_date(tz, resolved_now),
                    len(flights), len(all_flights),
                )
            else:
                flights = all_flights
                timezone_missing_airports.append(code)
                logger.warning(
                    "airport=%s için güvenilir timezone kaynağı YOK "
                    "(Airport.timezone boş veya zoneinfo'da tanınmıyor) - "
                    "operational-day filtresi UYGULANMADI, tüm geçmiş "
                    "flight'lar kullanıldı (eski davranış, limitation).",
                    code,
                )

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
                now=resolved_now,
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
        record_baseline_observations(session, total, now=resolved_now)

    return {
        "airports": per_airport,
        "predictions": len(total),
        "pruned": pruned,
        "failed_airports": failed_airports,
        "timezone_missing_airports": timezone_missing_airports,
        **written,
    }
