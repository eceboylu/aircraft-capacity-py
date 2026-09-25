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

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable
import json
import logging

from sqlalchemy import select, tuple_

from .baseline import existing_baseline_observation_keys, record_observation
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
from .core.event_queue import (
    DynamicStaffingParams,
    simulate_passport,
    simulate_security,
)
from .domain.dynamic_staffing import effective_capacity_by_hour
from .domain.operational_day import (
    filter_flights_for_operational_day,
    operational_date,
    resolve_airport_timezone,
)
from .domain.retention_time import USAGE_HORIZON_HOURS, canonical_flight_time
from .core.scoring import (
    confidence_score,
    domestic_security_capacity_rate,
    international_security_capacity_rate,
    passport_arrival_capacity_rate,
    passport_arrival_server_count,
    passport_capacity_rate,
    passport_departure_capacity_rate,
    passport_departure_server_count,
    passport_effective_server_count,
    passport_queue_model,
    risk_from_wait,
    security_capacity_rate,
    security_density_score,
    security_queue_model,
)
from .domain.demand import (
    DemandCalculator,
    arrival_passenger_release_events,
    departure_show_up_events,
    effective_time,
    flights_in_window,
)
from .domain.flows import (
    passport_arrival_flights,
    passport_departure_flights,
    passport_flights,
    security_domestic_flights,
    security_flights,
    security_international_flights,
)
from .models import Airport, Flight, HistoricalFlightCount, QueuePrediction
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
    # ADIM (Airport-Scale Queue Capacity) - departure/arrival passport
    # ARTIK AYRI fiziksel havuz (core/event_queue.py), bu yüzden ARTIK
    # kendi BAĞIMSIZ `_predict_window_core()` satırına sahipler - eski
    # `_passport_cohort_breakdown()` (birleşik PROCESS_PASSPORT'un
    # wait/risk'ini KOPYALAYAN yaklaşım) KALDIRILDI (bkz. git history).
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
)

# Hangi sürecin hangi uçuşlarla beslendiği (AŞAMA 2).
_PROCESS_FLIGHTS = {
    PROCESS_SECURITY: security_flights,
    PROCESS_PASSPORT: passport_flights,
    PROCESS_SECURITY_DOMESTIC: security_domestic_flights,
    PROCESS_SECURITY_INTL: security_international_flights,
    PROCESS_PASSPORT_DEPARTURE: passport_departure_flights,
    PROCESS_PASSPORT_ARRIVAL: passport_arrival_flights,
}

# `_predict_window_core()`'un PASSPORT dalının hangi fiziksel havuzu
# kullanacağı (bkz. core/scoring.py:passport_queue_model `pool` param).
# PROCESS_PASSPORT (birleşik/legacy) None -> eski `passport_effective_
# server_count()` (4x2=8 tarzı TEK sayı, backward-compat).
_PASSPORT_POOL_BY_PROCESS = {
    PROCESS_PASSPORT: None,
    PROCESS_PASSPORT_DEPARTURE: "departure",
    PROCESS_PASSPORT_ARRIVAL: "arrival",
}

# ADIM (Event-Driven Wait Reporting) - `estimated_wait_minutes`'ın
# GERÇEK ServiceEvent wait'inden (Erlang-C/fluid YERİNE) geldiği
# süreçler. Legacy PROCESS_PASSPORT/PROCESS_SECURITY (birleşik) BİLEREK
# DIŞARIDA - kullanıcı-visible wait'in tek kaynağı zaten SADECE bu
# dördü (api.py: domestic_security/international_departure/
# international_arrival grafikleri).
_EVENT_DRIVEN_WAIT_PROCESSES = frozenset({
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
})


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
    # ADIM (Current Operational Day Isolation) - bkz. `QueuePrediction.
    # operational_date` docstring'i (models.py). `predict_airport()`'ın
    # DOĞRUDAN çağıranları (çoğu mevcut test) bunu HİÇ SET ETMEZ - `None`
    # kalır (eski/tz'siz davranış). `run_predictions()` (bkz. altta)
    # KENDİ ürettiği her `WindowPrediction`'a, o havalimanı için
    # `operational_day.operational_date(tz, resolved_now)` sonucunu
    # SONRADAN atar - `predict_airport()`'ın imzası DEĞİŞTİRİLMEDİ.
    operational_date: date | None = None

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
    event_driven_wait_override: float | None = None,
    risk_backlog_start: float = 0.0,
    server_count_override: float | None = None,
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
    event_driven_wait_override
        : ADIM (Event-Driven Wait Reporting) - verilirse (None DEĞİLSE)
          `estimated_wait_minutes`, `queue_capacity_model()`'in ürettiği
          Erlang-C/fluid `wq` YERİNE bu GERÇEK, `ServiceEvent.wait_
          minutes`'tan türetilmiş passenger-ağırlıklı ortalamayla
          DEĞİŞTİRİLİR (bkz. `predict_airport`'un `event_wait_by_hour`
          kullanımı - SADECE PROCESS_PASSPORT_DEPARTURE/ARRIVAL ve
          PROCESS_SECURITY_DOMESTIC/INTL için doldurulur). `utilization`
          (SADECE `rho`'dan türer) bu override'dan ETKİLENMEZ. ADIM
          (Wait-Based Passenger Risk) SONRASI: `risk` bu override'dan
          SONRA, NİHAİ `estimated_wait_minutes` üzerinden `risk_from_
          wait()` ile YENİDEN hesaplanır (aşağıda) - `queue_capacity_
          model()`'in kendi "ham" `wq`'sundan türettiği risk artık
          override sonrası STALE olacağı için üzerine yazılır. Override
          verilmezse (None, legacy PROCESS_PASSPORT/PROCESS_SECURITY
          dahil diğer tüm süreçlerde hep None) `estimated_wait_minutes`
          (Erlang-C/fluid `wq`) birebir korunur, risk yine de AYNI
          `risk_from_wait()` çağrısıyla (artık bu sabit `wq`'dan)
          tutarlı şekilde hesaplanır.
    risk_backlog_start
        : ADIM (Visible Risk = Gerçek Queue Pressure) - `queue_capacity_
          model()`'e AYNEN iletilir (bkz. o fonksiyonun docstring'i) -
          SADECE `risk` sınıflandırmasının girdisi olan `queue_pressure`
          için kullanılır, `wq`/`utilization` bundan ETKİLENMEZ.
          Verilmezse (varsayılan 0.0, legacy PROCESS_PASSPORT/PROCESS_
          SECURITY VE doğrudan `predict_window()` çağıranları dahil
          diğer tüm yollarda hep 0.0) `queue_pressure == rho` olur -
          ESKİ risk davranışı birebir korunur.
    server_count_override
        : ADIM (Dynamic Capacity / Scoring Consistency) - verilirse
          (None DEĞİLSE), `config`'ten çözülen `server_count` YERİNE
          bu değer kullanılır - SADECE dynamic çalışan havuzlar (bkz.
          `domain/airport_scale.py:SCALE_RESOURCES` - şu an SADECE
          MEGA'nın `_max` anahtarı var) için `engine.py`'nin o pencere
          için hesapladığı GERÇEK, zaman-ağırlıklı aktif server
          ortalamasını (`domain/dynamic_staffing.py:effective_capacity_
          by_hour()`) taşımak amacıyla. `queue_pressure`/risk eşikleri/
          formülü HİÇ DEĞİŞMEDİ - sadece bu ÇAĞRIDA hangi kapasite
          SAYISININ kullanıldığı değişiyor. Verilmezse (None, statik
          kalan LARGE/MEDIUM/SMALL/UNKNOWN, override'lı MEGA alanları VE
          legacy PROCESS_PASSPORT dahil diğer TÜM yollarda hep None)
          eski davranış (config-derived, statik) birebir korunur.
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

    if process in _PASSPORT_POOL_BY_PROCESS:
        score = passport_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes,
            backlog_start=backlog_start,
            current_arrived_demand=current_arrived_demand,
            elapsed_minutes=elapsed_minutes,
            demand_override=demand_override,
            pool=_PASSPORT_POOL_BY_PROCESS[process],
            risk_backlog_start=risk_backlog_start,
            server_count_override=server_count_override,
        )
    else:
        score = security_queue_model(
            window_flights, config, demand.passenger_demand, window_minutes,
            backlog_start=backlog_start,
            current_arrived_demand=current_arrived_demand,
            elapsed_minutes=elapsed_minutes,
            demand_override=demand_override,
            lane_count_override=lane_count_override,
            risk_backlog_start=risk_backlog_start,
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

    # ADIM (Event-Driven Wait Reporting) - gerçek ServiceEvent-tabanlı
    # wait mevcutsa (bu pencerede en az 1 event varsa), Erlang-C/fluid
    # `wq`'nun YERİNE geçer. `utilization`/`risk` SADECE `rho`'dan
    # türediği için (yukarıda, `queue_capacity_model()` içinde) bu
    # override'dan HİÇ etkilenmez - sadece `estimated_wait_minutes`
    # değişir (Bölüm 15'in "risk eski Erlang-C wait'ten türemesin"
    # isteği zaten organik olarak sağlanmış oluyor, risk zaten wait'e
    # değil rho'ya bağlıydı).
    if event_driven_wait_override is not None:
        score["estimated_wait_minutes"] = round(event_driven_wait_override, 1)

    # ADIM (Wait-Based Passenger Risk) - `risk` her zaman NİHAİ (yukarıdaki
    # override sonrası) `estimated_wait_minutes`'ten türemeli - EVENT-DRIVEN
    # override varsa `queue_capacity_model()`'in KENDİ (henüz override
    # uygulanmamış) `wq`'sundan hesapladığı "ham" risk artık STALE olur;
    # bu satır risk'i GERÇEK, görünen wait ile YENİDEN hesaplayıp üzerine
    # yazar (override yoksa da koşulsuz çalışır - zaten aynı sonucu verir,
    # tek bir davranış yolu, iki ayrı risk mantığı YOK).
    score["risk"] = risk_from_wait(score["estimated_wait_minutes"])

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


# ADIM (Backlog-Aware Empty-Hour Reporting) - `_hourly_backlog_chain`'in
# son talep saatinden sonra backlog>0 kaldığı sürece zinciri UZATMASI
# için güvenlik tavanı (saat cinsinden). capacity_rate>0 garantisi
# altında backlog HER ek saatte kesin (service_capacity kadar) azaldığı
# için normal şartlarda asla tetiklenmez - SADECE teorik bir sonsuz
# döngüye karşı son çare. 24*30 = 30 gün, gerçekçi hiçbir backlog'un
# bunu aşması beklenmez (aşarsa zaten config gerçek dışıdır).
_MAX_BACKLOG_DRAIN_EXTENSION_HOURS = 24 * 30


def _hourly_backlog_chain(
    demand_by_hour: dict[datetime, float],
    capacity_rate: float | Callable[[datetime], float],
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

    ADIM (Analytic Backlog = Dynamic Capacity) - `capacity_rate` ARTIK
    sabit bir `float` OLMAK ZORUNDA DEĞİL - dynamic çalışan (şu an
    SADECE MEGA) passport havuzları için saat-bazlı DEĞİŞEN kapasiteyi
    yansıtan bir
    `Callable[[datetime], float]` da kabul eder (bkz. çağıran taraf -
    `_event_driven_queue_demand`'in `_dynamic_capacity_rate_fn()`'i).
    ESKİDEN bu fonksiyon HER ZAMAN `config.passport_departure_server_
    count`/`arrival_server_count`'un SABİT/TABAN değerinden türeyen TEK
    bir `capacity_rate` alıyordu - gerçek event-driven simülasyon
    (`simulate_fifo_queue_dynamic`) dynamic havuzlarda TABANDAN ÇOK
    DAHA YÜKSEK (max'a kadar) kapasiteyle çalışsa BİLE, bu analitik
    zincir hep düşük/sabit tabanı kullanıp GERÇEK olandan ÇOK DAHA
    BÜYÜK bir backlog RAPORLUYORDU (bkz. rapor - "IST canlı-veri
    denetiminde bulunan static-vs-dynamic capacity mismatch": gerçek
    simülasyonun kendi backlog'u ~250-950 iken bu zincirin ürettiği
    "analytic backlog" ~23,000-28,000'di). `float` veren ESKİ
    çağıranlar (security süreçleri, hiçbiri dynamic DEĞİL) HİÇ
    ETKİLENMEDİ - sadece dynamic passport departure/arrival artık
    KENDİ GERÇEK, saat-bazlı kapasitesini kullanıyor.

    İlk/son dolu saat arasındaki BOŞ saatler de dolaşılır (gişeler o
    saatlerde de servise devam eder) - `_queue_backlog_starts` ile AYNI
    ilke.

    ADIM (Backlog-Aware Empty-Hour Reporting) - eskiden zincir TAM
    OLARAK `starts[-1]`'de (son TALEP saati) dururdu - bu saatten sonra
    backlog>0 kalsa BİLE (ör. departure passport'un günün son show-up
    dalgası bitti ama devasa bir backlog hâlâ sunucularda işleniyor)
    zincir bu saatleri hiç ÜRETMİYORDU. Çağıran taraf (`predict_
    airport`) bu yüzden o saatler için hiç `WindowPrediction` üretmiyor,
    API katmanı da (`_pad_series_to_24_hours`) "talep yok -> LOW/0"
    sanıp SIFIR-TALEP padding'i basıyordu - GERÇEK, sürmekte olan
    backlog GİZLENİYORDU (bkz. rapor - Graph Bug #1). Şimdi zincir,
    `starts[-1]`'den SONRA da backlog kesinlikle 0'a inene kadar
    (`demand_here=0` varsayarak) DEVAM eder - bu, "gişeler boş saatlerde
    de servise devam eder" ilkesinin (yukarıdaki, mevcut) DOĞAL
    uzantısıdır, YENİ bir kavram değil. Underlying FIFO event-driven
    simülasyon (`core/event_queue.py`) HİÇ DEĞİŞMEDİ - bu SADECE fluid/
    analitik backlog zincirinin, simülasyonun ZATEN bildiği gerçeği
    (backlog boşalana kadar sunucular çalışır) ne kadar ileri saate
    kadar RAPORLADIĞI ile ilgili.
    """
    starts = sorted(demand_by_hour)
    if not starts:
        return {}

    capacity_rate_fn = capacity_rate if callable(capacity_rate) else (lambda _t: capacity_rate)
    backlog_start: dict[datetime, float] = {}

    backlog = 0.0
    current = starts[0]
    last = starts[-1]
    step = timedelta(minutes=window_minutes)
    extension_hours = 0

    while current <= last or backlog > 0:
        if current > last:
            if extension_hours >= _MAX_BACKLOG_DRAIN_EXTENSION_HOURS:
                break
            extension_hours += 1
        demand_here = demand_by_hour.get(current, 0.0)
        service_capacity = capacity_rate_fn(current) * window_minutes
        backlog_start[current] = backlog
        backlog = max(0.0, backlog + demand_here - service_capacity)
        current += step

    return backlog_start


def _event_derived_backlog_by_hour(events, starts) -> dict[datetime, float]:
    """
    ADIM (Visible Risk = Gerçek Queue Pressure) - Bölüm 2/4: bir pencere
    başlangıcı `T` için GERÇEK, event-türevli backlog - `_hourly_backlog_
    chain()`'in ürettiği fluid/analitik yaklaşıklığın YERİNE DEĞİL,
    YANINDA (o fonksiyon hâlâ DEĞİŞMEDEN WAIT hesabını besliyor) - SADECE
    `risk` sınıflandırması için ayrı, daha kesin bir backlog kaynağı.
    Yeni bir simülasyon ÇALIŞTIRMAZ - `_event_driven_queue_demand()`'ın
    ZATEN ürettiği `ServiceEvent` listesi üzerinde post-processing'dir.

    Tanım (Bölüm 2 - doğrulanmış): `T` anında HENÜZ SERVİSE BAŞLAMAMIŞ,
    önceki pencerelerden taşınan GERÇEK bekleyen yolcu sayısı:

        arrival_time < T  AND  service_start_time > T

    (sınırlar KASITLI asimetrik - `arrival_time < T` demektir "bu birim
    ÖNCEKİ bir pencerede kuyruğa girdi"; `T`'nin KENDİSİNDE giren
    birimler bu pencerenin KENDİ `arrivals_in_window`'udur, `_bucket_by_
    arrival`'ın aynı `T`'yi "bu pencereye ait" saydığı ayrım noktasıyla
    TUTARLI kesiliyor - bu yüzden bir birim ASLA hem backlog hem arrival
    olarak İKİ KEZ sayılmaz, bkz. `test_visible_risk_queue_pressure.py`
    double-count testi).

    `service_start_time <= T < completion_time` olan (T anında ZATEN
    servis alan, "in-service") birimler BİLİNÇLİ OLARAK DIŞLANMIŞTIR -
    residual-workload modellemesi (Bölüm 3) burada YAPILMADI:
    `service_time_minutes` (tipik 1-1.5 dk) `window_minutes`'a (60 dk)
    göre ihmal edilebilir küçük olduğu için bir sunucunun `T` sınırını
    aşan servisinin bıraktığı kalıntı iş en fazla `c * service_time_
    minutes` kişilik bir kapasite payıdır - bu, `service_capacity`'nin
    (`c * mu * window_minutes`) küçük, sınırlı bir kesridir; tam bir
    "kalan iş" modeli eklemek (ör. `completion_time - T` oranı)
    doğruluğu ölçülebilir şekilde artırmadan karmaşıklığı yükseltirdi
    (bkz. rapor - Bölüm 3 gerekçesi). Bu birimler backlog_start(T)'YE
    DAHİL EDİLMEZ.
    """
    return {
        start: sum(
            e.count for e in events
            if e.arrival_time < start and e.service_start_time > start
        )
        for start in starts
    }


def _dynamic_staffing_params_for(
    config: AirportConfigView, pool: str,
) -> DynamicStaffingParams | None:
    """
    ADIM (Dynamic MEGA Passport Staffing) - `pool` ("departure" veya
    "arrival") için, config'in ÇÖZÜLMÜŞ `passport_*_dynamic` bayrağı
    True ise `DynamicStaffingParams` (sabit kontrol parametreleriyle -
    Bölüm 2, RANDOM YOK) döner; False ise `None` döner - çağıran taraf
    (`simulate_passport`) `None`'ı "bu havuz sabit" olarak okur, eski
    `simulate_fifo_queue()` yoluna gider.

    ADIM (4-Tier Resource Contract) - bu bayrak SADECE `domain/airport_
    scale.py:SCALE_RESOURCES`'ta `_max` anahtarı TAŞIYAN tier'lar için
    `True` olabilir - bugünkü contract'ta bu TEK tier MEGA'dır (departure
    taban=30/tavan=45, arrival taban=35/tavan=45). LARGE/MEDIUM/SMALL
    HİÇBİR `_max` anahtarı TAŞIMAZ, bu yüzden bu üçü için `departure_max`/
    `arrival_max` HER zaman `None`, dynamic HER zaman `False` kalır -
    LARGE artık dynamic DEĞİLDİR (eski davranış, `_max` MEGA'ya taşındı).
    Kontrol KASITLI olarak `scale == "mega"` gibi sabit bir tier adına
    değil, `_max is not None`'a bağlı (scale-agnostik) - `_max` başka bir
    tier'a taşınır/eklenirse bu fonksiyon DEĞİŞTİRİLMEDEN doğru çalışır.
    """
    if pool == "departure":
        enabled = config.passport_departure_dynamic
        default = config.passport_departure_server_count
        maximum = config.passport_departure_server_count_max
    else:
        enabled = config.passport_arrival_dynamic
        default = config.passport_arrival_server_count
        maximum = config.passport_arrival_server_count_max

    if not enabled or maximum is None:
        return None

    return DynamicStaffingParams(
        default_server_count=default,
        max_server_count=maximum,
        control_interval_minutes=10,
        look_ahead_minutes=10,
        target_utilization=0.85,
        ramp_step=5,
    )


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
    from .domain.flows import (
        is_international_arrival,
        is_international_departure,
        is_schengen_departure_skipping_passport,
    )

    _now = now if now is not None else domain_now()

    def _arrival_release_arrivals(flight_list, predicate=None) -> list[tuple[datetime, float]]:
        """
        ADIM (Arrival Release Profile) - international arrival
        yolcularını artık TEK bir `effective_time()` (+15dk) noktası
        DEĞİL, her flight'ın KENDİ `arrival_passenger_release_events()`
        çıktısı (5 adet deterministic timestamp, HAM arrival zamanından
        - `effective_time()`'IN +15dk'sı ÜZERİNE İKİNCİ KEZ UYGULANMAZ,
        bkz. o fonksiyonun docstring'i) besler. SADECE `arrival_arrivals`
        için kullanılır - `departure_arrivals`/`domestic_arrivals`
        (`_departure_show_up_arrivals()`, aşağıda) bu fonksiyonu HİÇ
        ÇAĞIRMAZ - iki closure TAMAMEN BAĞIMSIZ (Bölüm 5/8: departure
        show-up implementasyonuna dokunulmadı).
        """
        result = []
        for f in flight_list:
            if f.status in EXCLUDED_STATUSES:
                continue
            if predicate is not None and not predicate(f):
                continue
            total = demand.passenger_demand(f)
            result.extend(arrival_passenger_release_events(f, total))
        return result

    def _departure_show_up_arrivals(flight_list, predicate=None) -> list[tuple[datetime, float]]:
        """
        ADIM (Departure Show-Up Profile) - Bölüm 1/3/7: departure
        yolcularını (international VEYA domestic - hangisi olduğu
        ÇAĞIRANIN verdiği `flight_list`/`predicate`'e bağlı) artık TEK
        bir `effective_time()` noktası DEĞİL, her flight'ın KENDİ
        `departure_show_up_events()` çıktısı besler - 4 adet 1 saatlik
        bandın (%10/%35/%45/%10), mutlak saat ızgarasına hizalı 5
        dakikalık bucket'larla GERÇEK interval overlap'i kullanılarak
        projekte edilmiş, deterministic event listesi (bkz. o
        fonksiyonun docstring'i - event sayısı flight'ın departure
        dakikasının saat ızgarasına hizalı olup olmamasına göre değişir,
        sabit bir sayı DEĞİLDİR). SADECE `departure_arrivals`/`domestic_
        arrivals` için kullanılır - `arrival_arrivals` bu fonksiyonu HİÇ
        ÇAĞIRMAZ (yukarıdaki `_arrivals()` ile üretilmeye devam eder).

        Her flight'ın batch'leri KENDİ departure zamanına göre
        üretildiği için farklı flight'ların batch'leri zaman ekseninde
        DOĞAL olarak üst üste biner - bu fonksiyon/`departure_show_up_
        events()` bunu KOORDİNE ETMEZ, sadece düz bir liste döner;
        kronolojik birleştirme/FIFO sıralaması `simulate_fifo_queue()`'nun
        (core/event_queue.py, DEĞİŞMEDİ) kendi işi.
        """
        result = []
        for f in flight_list:
            if f.status in EXCLUDED_STATUSES:
                continue
            if predicate is not None and not predicate(f):
                continue
            total = demand.passenger_demand(f)
            result.extend(departure_show_up_events(f, total))
        return result

    departure_arrivals = _departure_show_up_arrivals(passport_flights(flights), is_international_departure)
    arrival_arrivals = _arrival_release_arrivals(passport_flights(flights), is_international_arrival)
    domestic_arrivals = _departure_show_up_arrivals(security_domestic_flights(flights))

    # ADIM (Schengen-Aware Passport Routing) - `passport_flights()` ARTIK
    # Schengen->Schengen kalkışları HARİÇ TUTUYOR (bkz. domain/flows.py),
    # bu yüzden `departure_arrivals` bunları hiç İÇERMEZ - passport
    # demand'ine KATKI VERMEZLER (Bölüm 20/22). Ama bu yolcular international
    # security'yi HÂLÂ kullanır (Bölüm 9/13) - show-up zamanları DOĞRUDAN
    # security arrival_time'ı olur, passport completion_time'ından ASLA
    # TÜRETİLMEZ (Bölüm 23 - var olmayan bir passport gecikmesi icat
    # edilmez). `flights` (TÜM uçuşlar) üzerinden filtrelenir - `passport_
    # flights(flights)` DEĞİL, çünkü o küme bu uçuşları artık İÇERMİYOR.
    schengen_direct_security_arrivals = _departure_show_up_arrivals(
        flights, is_schengen_departure_skipping_passport
    )

    # ADIM (Airport-Scale Queue Capacity) - departure/arrival passport
    # ARTIK AYRI fiziksel havuz, kendi server sayısıyla (bkz.
    # core/event_queue.py:simulate_passport docstring'i).
    #
    # ADIM (Dynamic MEGA Passport Staffing) - `config.passport_
    # departure_dynamic`/`passport_arrival_dynamic` (config.py'nin
    # öncelik zincirinden - SADECE scale'in `SCALE_RESOURCES`'ta `_max`
    # anahtarı olması - bugün SADECE MEGA - VE airport-specific bir
    # override YOKSA True) True ise, sabit `passport_departure_server_
    # count(config)` SAYISI yerine `DynamicStaffingParams` inşa edilip
    # `simulate_passport`'a geçirilir - o havuz artık backlog+lookahead
    # demand'e göre 10dk'lık control noktalarında [default,max] arası
    # ayarlanır (bkz. `_dynamic_staffing_params_for` docstring'i).
    # LARGE/MEDIUM/SMALL/UNKNOWN VE override'lı MEGA alanları BU DALA
    # HİÇ GİRMEZ - `simulate_fifo_queue()`'nun ESKİ, sabit-server yoluna
    # aynen gider.
    passport_result = simulate_passport(
        departure_arrivals,
        arrival_arrivals,
        passport_departure_server_count(config),
        passport_arrival_server_count(config),
        config.passport_service_time_minutes,
        departure_dynamic=_dynamic_staffing_params_for(config, pool="departure"),
        arrival_dynamic=_dynamic_staffing_params_for(config, pool="arrival"),
    )

    # AŞAMA 9 - passport'un GERÇEK completion timestamp'i security'nin
    # arrival timestamp'i olur (saatlik-oransal tahmin DEĞİL).
    #
    # ADIM (Schengen-Aware Passport Routing, Bölüm 23/24) - Schengen->
    # Schengen kalkışlar `passport_result["departure"]`'da HİÇ YOK
    # (yukarıda filtrelendi), bu yüzden `schengen_direct_security_
    # arrivals` ile TOPLAMA hiçbir passenger'ı İKİ KEZ SAYMAZ - her
    # international kalkış yolcusu security_intl_arrivals'a TAM OLARAK
    # BİR KEZ girer: ya passport completion'ından (non-Schengen), ya da
    # kendi show-up zamanından (Schengen, doğrudan).
    security_intl_arrivals = [
        (event.completion_time, event.count) for event in passport_result["departure"]
    ] + schengen_direct_security_arrivals
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

    def _bucket_weighted_wait(events) -> dict[datetime, float]:
        """
        ADIM (Event-Driven Wait Reporting) - saatlik pencere için GERÇEK,
        passenger-ağırlıklı ortalama wait: `ServiceEvent.wait_minutes`
        (= service_start_time - arrival_time) `event.count` ile
        ağırlıklandırılıp `event.arrival_time`'a göre bucket'lanır (demand
        bucket'lamasıyla AYNI ilke - Bölüm 2: saatlik bucket SADECE
        reporting grouping'tir, queue hiçbir zaman saatlik adımlarla
        ilerlemedi, burada da ilerlemiyor).

        Bu saatte HİÇ event yoksa o saat için anahtar ÜRETİLMEZ (boş
        bucket'ta wait UYDURULMAZ - çağıran taraf `.get(start)` ile None
        alır, mevcut Erlang-C/fluid fallback'ine düşer - bkz. Bölüm 9).
        """
        weighted_sum: dict[datetime, float] = {}
        passenger_count: dict[datetime, float] = {}
        for event in events:
            key = floor_to_window(event.arrival_time, window_minutes)
            weighted_sum[key] = weighted_sum.get(key, 0.0) + event.wait_minutes * event.count
            passenger_count[key] = passenger_count.get(key, 0.0) + event.count
        return {
            key: weighted_sum[key] / passenger_count[key]
            for key in weighted_sum
            if passenger_count[key] > 0
        }

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
        # Birleşik/legacy - artık İKİ AYRI fiziksel havuzun BASİT
        # birleşimi (raporlama amaçlı, kendi server'ı YOK - bkz.
        # capacity_rates altında toplamı).
        PROCESS_PASSPORT: passport_result["departure"] + passport_result["arrival"],
        PROCESS_PASSPORT_DEPARTURE: passport_result["departure"],
        PROCESS_PASSPORT_ARRIVAL: passport_result["arrival"],
        PROCESS_SECURITY_DOMESTIC: security_dom_events,
        PROCESS_SECURITY: security_combined_events,
        PROCESS_SECURITY_INTL: security_intl_events,
    }

    demand_by_hour = {
        process: _bucket_by_arrival(events)
        for process, events in process_events.items()
    }

    # PROCESS_PASSPORT (birleşik/legacy) KASITLI olarak ESKİ `passport_
    # capacity_rate()` (4x2=8 tarzı, `passport_counter_count x passport_
    # staff_per_counter`'dan) formülünü KULLANMAYA DEVAM EDER - departure+
    # arrival kapasitelerini TOPLAMAK yanlış olurdu: demand departure/
    # arrival arasında EŞİT DAĞILMIYORSA (ör. saf-arrival bir pencere),
    # "iki havuzun toplam kapasitesi" o TEK havuza (arrival'a) hiç
    # AKTARILAMAYAN fazladan kapasite varsayardı - backlog'u OLMADIĞI
    # kadar hızlı boşaltırdı (bkz. test_passport_backlog_model.py'nin
    # gerçek regresyon bulgusu). Legacy alan bu yüzden ESKİ, TEK
    # (`passport_counter_count`/`passport_staff_per_counter`) sayıyla
    # DEĞİŞMEDEN hesaplanmaya devam eder; departure/arrival'ın KENDİ
    # GERÇEK kapasiteleri SADECE PROCESS_PASSPORT_DEPARTURE/ARRIVAL'da.
    capacity_rates: dict[str, float | Callable[[datetime], float]] = {
        PROCESS_PASSPORT: passport_capacity_rate(config),
        PROCESS_PASSPORT_DEPARTURE: passport_departure_capacity_rate(config),
        PROCESS_PASSPORT_ARRIVAL: passport_arrival_capacity_rate(config),
        PROCESS_SECURITY_DOMESTIC: domestic_security_capacity_rate(config),
        PROCESS_SECURITY: security_capacity_rate(config),
        PROCESS_SECURITY_INTL: international_security_capacity_rate(config),
    }

    # ADIM (Analytic Backlog = Dynamic Capacity) - dynamic çalışan (şu an
    # SADECE MEGA) passport havuzları için PROCESS_PASSPORT_DEPARTURE/
    # ARRIVAL'ın yukarıdaki SABİT (taban server sayısından türeyen)
    # `capacity_rate`'i, o havuzun GERÇEK event-driven simülasyonunun
    # (`simulate_fifo_queue_dynamic`) o saat ne kullandığına göre
    # DEĞİŞEN bir fonksiyonla DEĞİŞTİRİLİR - `_hourly_backlog_chain`'in
    # ("analytic" backlog, reported wait'i besler) REAL SIMÜLASYONUN
    # KENDİSİYLE AYNI kapasiteyi kullanması için (bkz. rapor - static-
    # vs-dynamic capacity mismatch bulgusu). Dynamic DEĞİLSE (`schedule
    # is None` - LARGE/MEDIUM/SMALL/UNKNOWN veya explicit override'lı
    # MEGA) yukarıdaki SABİT değer AYNEN kalır - davranış HİÇ DEĞİŞMEZ.
    def _dynamic_capacity_rate_fn(
        schedule: list[tuple[datetime, int]],
    ) -> Callable[[datetime], float]:
        service_rate_per_server_per_minute = 1.0 / config.passport_service_time_minutes
        ordered = sorted(schedule, key=lambda item: item[0])

        def _rate(hour_start: datetime) -> float:
            eff = effective_capacity_by_hour(ordered, [hour_start], window_minutes)
            active = eff.get(hour_start, ordered[0][1])
            return active * service_rate_per_server_per_minute

        return _rate

    departure_schedule = passport_result.get("departure_schedule")
    if departure_schedule is not None:
        capacity_rates[PROCESS_PASSPORT_DEPARTURE] = _dynamic_capacity_rate_fn(departure_schedule)
    arrival_schedule = passport_result.get("arrival_schedule")
    if arrival_schedule is not None:
        capacity_rates[PROCESS_PASSPORT_ARRIVAL] = _dynamic_capacity_rate_fn(arrival_schedule)

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

    # ADIM (Event-Driven Wait Reporting) - GERÇEK, ServiceEvent-tabanlı
    # passenger-ağırlıklı ortalama wait, saate bucket'lanmış. `predict_
    # airport()` bunu SADECE PROCESS_PASSPORT_DEPARTURE/ARRIVAL ve
    # PROCESS_SECURITY_DOMESTIC/INTL için `_predict_window_core()`'a
    # `event_driven_wait_override` olarak geçirir - legacy PROCESS_
    # PASSPORT/PROCESS_SECURITY (birleşik) bu turda KASITLI olarak
    # dokunulmadı (eski Erlang-C/fluid davranışı korunur).
    event_wait_by_hour = {
        process: _bucket_weighted_wait(events)
        for process, events in process_events.items()
    }

    return {
        "demand_by_hour": demand_by_hour,
        "backlog_start_by_hour": backlog_start_by_hour,
        "current_released_by_hour": current_released_by_hour,
        "event_wait_by_hour": event_wait_by_hour,
        # ADIM (Visible Risk = Gerçek Queue Pressure) - ham ServiceEvent
        # listeleri (process başına) - `predict_airport()`'un `_event_
        # derived_backlog_by_hour()` çağırması için. Yeni bir simülasyon
        # DEĞİL, sadece ZATEN hesaplanmış event'lerin dışa aktarılması.
        "process_events": process_events,
        # ADIM (Dynamic Capacity / Scoring Consistency) - `passport_
        # result["departure_schedule"]`/`["arrival_schedule"]` (dynamic
        # DEĞİLSE `None`) - `predict_airport()`'un `effective_capacity_
        # by_hour()` çağırıp SADECE PROCESS_PASSPORT_DEPARTURE/ARRIVAL
        # için `server_count_override` üretmesi için. Yeni bir simülasyon
        # DEĞİL, `simulate_passport()`'un ZATEN döndürdüğü programın
        # dışa aktarılması.
        "passport_schedules": {
            PROCESS_PASSPORT_DEPARTURE: passport_result.get("departure_schedule"),
            PROCESS_PASSPORT_ARRIVAL: passport_result.get("arrival_schedule"),
        },
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
    # Hepsi burada erken doğrulanır (BUG-03 davranışı korunuyor) - hiçbiri
    # aşağıdaki event-driven simülasyon içinde SESSİZCE patlamaz. ADIM
    # (Airport-Scale Queue Capacity): departure/arrival passport AYRI
    # havuz olduğu için İKİSİ de AYRI doğrulanır (biri geçersizken
    # diğeri geçerli olabilir - ör. sadece departure override 0 verildi).
    try:
        passport_capacity_rate(config)
        passport_departure_capacity_rate(config)
        passport_arrival_capacity_rate(config)
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
        coupled_wait = coupling["event_wait_by_hour"][process]
        # starts = RAW uçuş pencereleri (flight_count/neden tespiti için,
        # DEĞİŞMEDİ) BİRLEŞİMİ simülasyonun ürettiği GERÇEK saatlerle -
        # ikisi FARKLI olabilir (ör. passport geç serbest bıraktığı için
        # security'ye ancak bir SONRAKİ saatte ulaşan yolcular; PASSPORT/
        # SECURITY_DOMESTIC için ikisi PRATİKTE AYNIDIR çünkü arrival_time
        # zaten flight'ın kendi effective_time'ıdır).
        #
        # ADIM (Backlog-Aware Empty-Hour Reporting) - `set(coupled_
        # backlog)` EKLENDİ: `_hourly_backlog_chain` artık son talep
        # saatinden sonra backlog>0 kaldığı sürece ek saatler üretiyor
        # (bkz. o fonksiyonun docstring'i) - bu saatler `buckets`'ta da
        # `coupled_demand`'da da YOKTUR (hiç yeni uçuş/arrival yok), bu
        # yüzden onlar olmadan `starts` bu saatleri hiç İÇERMEZ ve o
        # saat için WindowPrediction hiç ÜRETİLMEZ - API katmanı sonra
        # bunu "talep yok" sanıp sıfır-talep padding'i basar (Graph
        # Bug #1). `coupled_backlog`'u birliğe eklemek, backlog varken
        # HİÇBİR saatin sessizce atlanmamasını garantiler.
        starts = sorted(set(buckets) | set(coupled_demand) | set(coupled_backlog))

        # ADIM (Visible Risk = Gerçek Queue Pressure) - GERÇEK, event-
        # türevli backlog SADECE 4 görünür/event-driven-wait sürecinde
        # (`_EVENT_DRIVEN_WAIT_PROCESSES`) hesaplanır - legacy PROCESS_
        # PASSPORT/PROCESS_SECURITY (birleşik) bu turda KASITLI olarak
        # DOKUNULMADI (Bölüm 12 - legacy visible risk'e sızmasın; zaten
        # visible 5 grafiğin hiçbiri bunları kullanmıyor).
        risk_backlog_by_hour = (
            _event_derived_backlog_by_hour(coupling["process_events"][process], starts)
            if process in _EVENT_DRIVEN_WAIT_PROCESSES else {}
        )

        # ADIM (Dynamic Capacity / Scoring Consistency) - SADECE
        # PROCESS_PASSPORT_DEPARTURE/ARRIVAL için (VE SADECE o havuz
        # gerçekten dynamic çalıştıysa - `passport_schedules[process]`
        # None DEĞİLSE): `queue_capacity_model()`'in `server_count`'ını
        # SABİT config değeri yerine, simülasyonun O SAAT için GERÇEKTEN
        # çalıştırdığı zaman-ağırlıklı ortalama aktif server sayısıyla
        # (`effective_capacity_by_hour()`) değiştir - simüle edilen
        # kapasite ile raporlanan `utilization`/`queue_pressure` AYNI
        # operasyonel gerçeği temsil etsin (Bölüm 9/10).
        effective_servers_by_hour: dict = {}
        schedule = coupling.get("passport_schedules", {}).get(process)
        if schedule is not None:
            effective_servers_by_hour = effective_capacity_by_hour(
                schedule, starts, window_minutes
            )

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
                event_driven_wait_override=(
                    coupled_wait.get(start)
                    if process in _EVENT_DRIVEN_WAIT_PROCESSES else None
                ),
                risk_backlog_start=risk_backlog_by_hour.get(start, 0.0),
                server_count_override=effective_servers_by_hour.get(start),
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


def flights_of_airport(
    session, airport_iata: str, now: datetime | None = None,
    horizon_hours: int = USAGE_HORIZON_HOURS,
) -> list[Flight]:
    """
    Sadece bu havalimanının uçuşları - başka havalimanı karışmaz.

    ADIM (48h Usage Horizon) - Bölüm 8: `now` verilirse (production
    `run_predictions()` ZATEN `resolved_now`'ı geçiyor), canonical
    operasyonel zamanı (`domain/retention_time.py` - flight_key ile AYNI
    departure/arrival seçim kuralı) `now - horizon_hours` cutoff'undan
    ESKİ olan satırlar dönüşe DAHİL EDİLMEZ. Bu, timezone'u çözülemeyen
    havalimanları için var olan "tüm geçmiş flight kullanılıyor"
    fallback'ini (bkz. `run_predictions`) sınırsız yerine 48 saatle
    sınırlar - bugünkü operasyonel-gün filtresi (timezone çözülebilen
    havalimanlar için) ZATEN bundan çok daha dar olduğu için (en erken
    pencere başlangıcı `now`'dan en fazla 24 saat geride olabilir,
    bkz. `operational_day_window()`), bu güvenlik ağı O YOLU HİÇ
    ETKİLEMEZ - sadece "tüm geçmiş" fallback'ini sınırlar.

    Canonical zaman None ise (scheduled bilinmiyor) satır KORUNUR -
    bilinmeyen bir zaman "eski" sayılıp sessizce atılmaz.

    `now=None` (varsayılan - mevcut/eski çağıranlar) ise HİÇBİR filtre
    uygulanmaz, ESKİ davranış (TÜM satırlar) birebir korunur - geriye
    dönük tam uyumlu.
    """
    rows = list(session.execute(
        select(Flight).where(Flight.airport_iata == airport_iata)
    ).scalars().all())

    if now is None:
        return rows

    cutoff = now - timedelta(hours=horizon_hours)
    result = []
    for flight in rows:
        canonical = canonical_flight_time(flight)
        if canonical is None or canonical >= cutoff:
            result.append(flight)
    return result


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


def _load_historical_flight_count_cache(
    session, airport_iata: str
) -> dict[tuple[str, int, int], HistoricalFlightCount]:
    """
    ADIM (MySQL Performance Regression Fix - Phase 1) - Bölüm 11-14:
    bu havalimanı için `HistoricalFlightCount`'un TÜM satırlarını TEK
    bir bulk SELECT ile yükler - `_bucket()`'ın (baseline.py) kullandığı
    AYNI dört boyutla (`airport_iata`, `process`, `hour_of_day`,
    `day_of_week`) anahtarlanan bir sözlük döner; `airport_iata` bu
    fonksiyonun kendi filtresi olduğu için sözlük anahtarı SADECE
    (process, hour_of_day, day_of_week) - havalimanı zaten SABİT
    (çapraz-havalimanı SIZINTI YAPISAL OLARAK imkansız, bkz. `WHERE
    airport_iata == airport_iata`).

    Bölüm 14 (yazma sırası/point-in-time semantiği) - GÜVENLİ: bu
    cache `predict_airport()` çağrılmadan HEMEN ÖNCE, bu havalimanı
    için BİR KEZ yüklenir; `record_baseline_observations()` (bu
    döngünün YAZDIĞI tek yer) TÜM havalimanlarının tahminleri
    üretildikten SONRA, `run_predictions()`'ın en sonunda çağrılır -
    yani bu cache'in ömrü boyunca `HistoricalFlightCount`'a HİÇ yazma
    OLMAZ; eski satır-başına-SELECT yaklaşımı da AYNI garantiyi (bu
    turun KENDİ yazmalarını asla erken GÖRMEZ) doğal olarak sağlıyordu -
    cache bu semantiği DEĞİŞTİRMEZ, sadece AYNI sonucu tek sorguyla üretir.
    """
    rows = session.execute(
        select(HistoricalFlightCount).where(HistoricalFlightCount.airport_iata == airport_iata)
    ).scalars().all()
    return {(r.process, r.hour_of_day, r.day_of_week): r for r in rows}


def _db_baseline_fn(session, airport_iata: str, cache: dict | None = None):
    """Uçuş baseline okumasını (süreç, pencere) çiftine bağlar - `cache` verilmezse bu airport için BİR KEZ yüklenir (bkz. `_load_historical_flight_count_cache`)."""
    if cache is None:
        cache = _load_historical_flight_count_cache(session, airport_iata)

    def lookup(process: str, window_start: datetime) -> float | None:
        row = cache.get((process, window_start.hour, window_start.weekday()))
        if row is None or row.sample_size <= 0:
            return None
        return row.average_flight_count
    return lookup


def _db_passenger_baseline_fn(session, airport_iata: str, cache: dict | None = None):
    """MADDE 7 - yolcu baseline okumasını (süreç, pencere) çiftine bağlar - AYNI `cache`'i `_db_baseline_fn` ile PAYLAŞABİLİR (iki AYRI bulk SELECT gerekmez, ikisi de AYNI HistoricalFlightCount satırlarını okur)."""
    if cache is None:
        cache = _load_historical_flight_count_cache(session, airport_iata)

    def lookup(process: str, window_start: datetime) -> float | None:
        row = cache.get((process, window_start.hour, window_start.weekday()))
        if row is None or row.passenger_sample_size <= 0:
            return None
        return row.average_expected_passengers
    return lookup


# ADIM (MySQL Performance Fix - Phase 2) - `persist_predictions()`'ın
# bulk existing-row prefetch chunk boyutu. `refresh.py:REFRESH_CHUNK_SIZE`
# (500) ile AYNI ölçek - ayrı bir modülün kendi sabiti, paylaşılan/
# import edilen bir değer DEĞİL (modüller BİLEREK ayrık, bkz. refresh.py
# başlığı - aynı gerekçe).
PREDICTION_PERSIST_CHUNK_SIZE = 500

# `persist_predictions()`'ın no-op karşılaştırmasına KATILAN alanlar -
# `calculated_at` BİLEREK DIŞARIDA (bkz. fonksiyonun kendi docstring'i,
# Bölüm "no-op skip AYNI zamanda calculated_at'i de dondurur").
_PREDICTION_COMPARE_COLUMNS = (
    "window_end",
    "operational_date",
    "flight_count",
    "expected_passengers",
    "baseline_ratio",
    "flight_ratio",
    "passenger_ratio",
    "utilization",
    "estimated_wait_minutes",
    "risk",
    "confidence",
)


def _prediction_as_candidate_values(prediction: WindowPrediction) -> dict:
    """`WindowPrediction` -> QueuePrediction kolon değerleri (id/
    calculated_at HARİÇ) - INSERT'te VE no-op karşılaştırmasında
    (`_prediction_differs`) TEK gerçek kaynak, iki yerde ayrı yazılan
    bir alan listesi YOK."""
    return {
        "window_end": prediction.window_end,
        "operational_date": prediction.operational_date,
        "flight_count": prediction.flight_count,
        "expected_passengers": prediction.expected_passengers,
        "baseline_ratio": prediction.baseline_ratio,
        "flight_ratio": prediction.flight_ratio,
        "passenger_ratio": prediction.passenger_ratio,
        "utilization": prediction.utilization,
        "estimated_wait_minutes": prediction.estimated_wait_minutes,
        "risk": prediction.risk,
        "reasons": prediction.reasons_json(),
        "confidence": prediction.confidence,
    }


def _prediction_differs(existing: QueuePrediction, prediction: WindowPrediction) -> bool:
    """
    ADIM (MySQL Performance Fix - Phase 2) - `refresh.py:
    _row_differs_from_existing()` ile AYNI ilke (Step 4/21 "no-op
    update skip"), QueuePrediction için: `existing`'in GÜNCEL alan
    değerlerinden (id/calculated_at HARİÇ) HERHANGİ biri `prediction`'ın
    (yeniden hesaplanmış) değerinden farklıysa True. `reasons` JSON
    string olarak (`reasons_json()` - ÜRETİMİ/serileştirmesi HİÇ
    DEĞİŞMEDİ) karşılaştırılır - üretim/persist edilen DEĞER aynı,
    sadece YAZILIP YAZILMAYACAĞINA karar verilir.

    Float alanlar (`baseline_ratio`/`utilization`/`estimated_wait_
    minutes`/`confidence`/vb.) için ekstra tolerans/rounding
    EKLENMEDİ - `core/scoring.py` bu değerleri ZATEN sabit ondalık
    basamağa yuvarlanmış üretir (`round(rho,3)`, `round(wq,1)` vb.) ve
    MySQL FLOAT round-trip'i (doğrudan gerçek MySQL'de doğrulandı) bu
    zaten-yuvarlanmış değerler için tam eşitliği KORUR - "mevcut output
    semantics'i değiştirecek rounding ekleme" kuralına uyulur.
    """
    for column in _PREDICTION_COMPARE_COLUMNS:
        if getattr(existing, column) != getattr(prediction, column):
            return True
    if existing.reasons != prediction.reasons_json():
        return True
    return False


def _apply_prediction_fields(existing: QueuePrediction, prediction: WindowPrediction, now: datetime) -> None:
    """Gerçekten değişen (veya yeni eklenen) bir satıra TÜM alanları
    yazar - `calculated_at` SADECE bu fonksiyon çağrıldığında ilerler
    (bkz. `persist_predictions()` no-op skip)."""
    values = _prediction_as_candidate_values(prediction)
    for column, value in values.items():
        setattr(existing, column, value)
    existing.calculated_at = now


def persist_predictions(session, predictions: list[WindowPrediction]) -> dict:
    """
    QueuePrediction UPSERT: (havalimanı, süreç, pencere) başına TEK
    satır. Aynı pencere tekrar hesaplanırsa satır güncellenir, yenisi
    açılmaz (YASAK 4).

    ADIM (MySQL Performance Fix - Phase 2) - Bölüm: 10k MySQL scale
    benchmark'ında doğrulanan iki bottleneck'ten biri buradaydı:
    ÖNCEDEN (a) her prediction için AYRI bir SELECT (`existing_by_key`
    şimdi `refresh.py:_bulk_fetch_existing()` ile AYNI ilkeyle TEK bir
    bulk sorguda - `tuple_(...).in_(...)` - önceden yükleniyor), (b)
    eşleşen satırın TÜM alanları HER ZAMAN yeniden yazılıyordu (`calculated_
    at` dahil - bu, DEĞER hiç değişmese de her cycle'da GERÇEK bir SQL
    UPDATE'e yol açıyordu, bkz. 10k benchmark: identical cycle'da 1858
    prediction için 1859 UPDATE). Artık `_prediction_differs()` ile
    karşılaştırılıp GERÇEKTEN değişen satırlar YAZILIR - değişmeyenler
    (no-op) `existing`'e HİÇ dokunmaz, DB'ye HİÇBİR UPDATE gitmez.

    `calculated_at` no-op'ta DONAR (ilerlemez) - `_apply_prediction_
    fields()` SADECE gerçek bir değişiklik/yeni satır olduğunda
    çağrılır. Bu, `refresh.py`'nin `last_refreshed_at` için ZATEN
    kurduğu AYNI, kabul edilmiş presedanla BİREBİR TUTARLIDIR (bkz. o
    modülün `_apply_candidate()` docstring'i - "no-op skip sadece DB
    YAZIMINI atlar" ilkesi `updated` SAYACINA da AYNI şekilde uygulanır,
    aşağıda).

    `inserted`/`updated` dönüş sözleşmesi DEĞİŞMEDİ: `updated`, bir
    prediction MEVCUT bir satırla eşleştiğinde artırılır - GERÇEK bir
    DB yazımı olup olmadığından BAĞIMSIZ (refresh.py'nin `updated`
    sayacıyla AYNI konvansiyon - "eşleşti" ile "DB'ye yazıldı" AYRI
    kavramlar).
    """
    now = datetime.now(timezone.utc)
    inserted = 0
    updated = 0

    if not predictions:
        session.commit()
        return {"inserted": 0, "updated": 0}

    keys = [
        (p.airport_iata, p.process, p.window_start) for p in predictions
    ]
    existing_by_key: dict[tuple[str, str, datetime], QueuePrediction] = {}
    key_tuple = tuple_(
        QueuePrediction.airport_iata, QueuePrediction.process, QueuePrediction.window_start
    )
    for start in range(0, len(keys), PREDICTION_PERSIST_CHUNK_SIZE):
        chunk_keys = keys[start:start + PREDICTION_PERSIST_CHUNK_SIZE]
        rows = session.execute(
            select(QueuePrediction).where(key_tuple.in_(chunk_keys))
        ).scalars().all()
        for row in rows:
            existing_by_key[(row.airport_iata, row.process, row.window_start)] = row

    for prediction in predictions:
        key = (prediction.airport_iata, prediction.process, prediction.window_start)
        existing = existing_by_key.get(key)

        if existing is None:
            values = _prediction_as_candidate_values(prediction)
            new_row = QueuePrediction(
                airport_iata=prediction.airport_iata,
                process=prediction.process,
                window_start=prediction.window_start,
                calculated_at=now,
                **values,
            )
            session.add(new_row)
            # Aynı (airport_iata, process, window_start) `predictions`
            # listesinde (nadiren) TEKRAR gelirse ikinci INSERT değil
            # ikinci eşleşme (UPDATE-veya-no-op yolu) olsun - `refresh.
            # py:_process_chunk()`'ın AYNI, mevcut, kanıtlanmış deseni.
            existing_by_key[key] = new_row
            inserted += 1
            continue

        updated += 1
        if _prediction_differs(existing, prediction):
            _apply_prediction_fields(existing, prediction, now)
        # else: no-op - existing'e HİÇ dokunulmaz, DB'ye UPDATE gitmez
        # (calculated_at dahil - bkz. fonksiyon docstring'i).

    session.commit()
    return {"inserted": inserted, "updated": updated}


def prune_stale_predictions(
    session,
    airport_iata: str,
    keep: set[tuple[str, datetime]],
    operational_date_value: date | None = None,
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

    ADIM (Current Operational Day Isolation) - BUG FIX: `keep`, BU
    ÇAĞRIDA sadece havalimanının BUGÜNKÜ (yerel) operasyonel gününe ait
    flight'lardan üretildi (bkz. `run_predictions` - operational-day
    filtresi). Eskiden bu fonksiyon havalimanının TÜM (her gün, her
    süreç) satırlarını tarayıp `keep`'te olmayan HERŞEYİ siliyordu -
    bu, persistan/çok-günlü bir DB'de (Bölüm 6 - "OLD DATA KAYBOLMASIN")
    ÖNCEKİ GÜNLERİN GERÇEK geçmişini, o güne ait YENİ bir flight/
    prediction üretildiği AN silinmesine yol açıyordu (kendi günü
    dışındaki hiçbir satır zaten `keep`'te olamaz).

    `operational_date_value` verilirse (timezone çözülebilen bir
    havalimanı) tarama SADECE `operational_date == operational_date_value`
    satırlarıyla SINIRLANIR - böylece diğer günlerin satırları bu
    fonksiyona hiç GÖRÜNMEZ, silinemez. `None` ise (timezone çözülemeyen
    havalimanı - Bölüm 59 limitation, eski davranış) tarama ESKİ, TÜM
    satırlar üzerinde çalışır - GERİYE DÖNÜK UYUMLU.
    """
    query = select(QueuePrediction).where(QueuePrediction.airport_iata == airport_iata)
    if operational_date_value is not None:
        query = query.where(QueuePrediction.operational_date == operational_date_value)
    rows = session.execute(query).scalars().all()

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

    ADIM (MySQL Performance Fix - Phase 2) - 10k MySQL scale benchmark'ında
    doğrulanan ikinci bottleneck buradaydı: kapanmış HER pencere için
    `record_observation()` KOŞULSUZ çağrılıyordu - zaten defterde kayıtlı
    (önceki bir cycle'da işlenmiş) bir üçlü için bu, GARANTİ bir INSERT
    + `IntegrityError` + `rollback` + skip anlamına geliyordu (identical
    cycle'da 105/105 pencere için, bkz. benchmark: 106 rollback/cycle).
    Artık `existing_baseline_observation_keys()` ile (bkz. baseline.py)
    kapanmış pencerelerin üçlüleri TEK bulk sorguda önceden çekilir;
    ZATEN kayıtlı üçlüler için `record_observation()` HİÇ ÇAĞRILMAZ
    (INSERT denemesi/IntegrityError/rollback ÜRETİLMEZ). Bu SADECE bir
    performans ön-kontrolüdür - GERÇEK bir race (iki eşzamanlı worker
    AYNI üçlüyü AYNI anda işlerse) `record_observation()`'ın KENDİ,
    DEĞİŞTİRİLMEMİŞ unique-constraint/IntegrityError savunma hattı HÂLÂ
    devrededir (bkz. o fonksiyonun docstring'i) - doğruluk garantisi
    performans için FEDA EDİLMEDİ, sadece normal (race'siz) tekrar
    cycle'ındaki GEREKSİZ deneme kaldırıldı. `HistoricalFlightCount`'un
    hareketli ortalama/upsert matematiği (`record_observation()`'ın
    KENDİSİ) HİÇ DEĞİŞMEDİ.
    """
    now = now if now is not None else domain_now()
    recorded = 0

    closed_predictions = [p for p in predictions if now >= p.window_end]
    closed_keys = [
        (p.airport_iata, p.process, p.window_start) for p in closed_predictions
    ]
    already_recorded = existing_baseline_observation_keys(session, closed_keys)

    old_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        for prediction in closed_predictions:
            key = (prediction.airport_iata, prediction.process, prediction.window_start)
            if key in already_recorded:
                continue  # bu üçlü ZATEN defterde - INSERT denemesi gereksiz
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
    apply_usage_horizon: bool = False,
) -> dict:
    """
    Sistemdeki HER havalimanı için tahminleri üretir ve kaydeder.

    resolver : Madde 1'in AircraftCapacityService örneği. Bu servis
               burada DEĞİŞTİRİLMEZ, sadece kullanılır.
    now      : MADDE 3 - açık/kapalı pencere kontrolü için "şu an".
               Test edilebilirlik için enjekte edilebilir; verilmezse
               domain_now() (gerçek saat) kullanılır.
    apply_usage_horizon : ADIM (48h Usage Horizon) - Bölüm 8. Varsayılan
               `False` - MEVCUT davranış (TÜM Flight geçmişi, sadece
               operational-day filtresiyle daraltılır) birebir korunur;
               bu, sabit/geçmiş tarihli fixture'larla (gerçek `now`
               verilmeden) çalışan MEVCUT testlerin bozulmaması için
               BİLİNÇLİ bir tercih (48h filtresi `resolved_now`'ı HER
               ZAMAN gerçek/kararlı bir "şu an" sayardı - ama `now=None`
               olduğunda `resolved_now` gerçek duvar saatidir ve eski
               fixture verisiyle UYUŞMAZ). `True` verilirse (SADECE
               `app/worker.py`'nin gerçek production çağrısı bunu yapar)
               `flights_of_airport()`'un 48h usage-horizon filtresi
               AKTİFLEŞİR - timezone'u çözülemeyen havalimanları için
               var olan "tüm geçmiş kullanılıyor" fallback'ini sınırsız
               yerine 48 saatle sınırlar (bkz. rapor).

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
            all_flights = flights_of_airport(
                session, code, now=resolved_now if apply_usage_horizon else None,
            )
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
            airport_operational_date: date | None = None
            if tz is not None:
                flights = filter_flights_for_operational_day(
                    all_flights, tz, resolved_now
                )
                airport_operational_date = operational_date(tz, resolved_now)
                logger.info(
                    "operational-day filtresi uygulandı (airport=%s, "
                    "local_date=%s, %d/%d uçuş seçildi)",
                    code, airport_operational_date,
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

            # ADIM (MySQL Performance Regression Fix - Phase 1) - Bölüm 11/12:
            # bu havalimanının HistoricalFlightCount satırları BİR KEZ
            # yüklenir, `baseline_fn`/`passenger_baseline_fn` İKİSİ DE AYNI
            # cache'i paylaşır (iki ayrı bulk SELECT yerine tek sorgu).
            historical_cache = _load_historical_flight_count_cache(session, code)
            predictions = predict_airport(
                airport_iata=code,
                flights=flights,
                config=configs[code],
                demand=DemandCalculator(resolver),
                baseline_fn=_db_baseline_fn(session, code, cache=historical_cache),
                passenger_baseline_fn=_db_passenger_baseline_fn(session, code, cache=historical_cache),
                aircraft_changes=aircraft_changes_for_airport(session, code),
                window_minutes=window_minutes,
                now=resolved_now,
            )
            # ADIM (Current Operational Day Isolation) - bu havalimanı için
            # ÇÖZÜLEN yerel "bugün" (`airport_operational_date`, tz
            # çözülemiyorsa None) HER `WindowPrediction`'a etiketlenir -
            # `predict_airport()`'ın kendisi bunu BİLMEZ/HESAPLAMAZ, sadece
            # burada, ÜRETİLDİKTEN SONRA atanır (bkz. `WindowPrediction.
            # operational_date` docstring'i).
            for p in predictions:
                p.operational_date = airport_operational_date
            per_airport[code] = len(predictions)
            total.extend(predictions)

            pruned += prune_stale_predictions(
                session,
                code,
                {(p.process, p.window_start) for p in predictions},
                operational_date_value=airport_operational_date,
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
