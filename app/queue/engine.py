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
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    REASON_CAPACITY_EXCEEDED,
    REASON_NO_BASELINE,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
)
from .core.scoring import (
    confidence_score,
    passport_capacity_rate,
    passport_queue_model,
    security_capacity_rate,
    security_density_score,
    security_queue_model,
)
from .domain.demand import DemandCalculator, effective_time, flights_in_window
from .domain.flows import (
    passport_flights,
    security_domestic_flights,
    security_flights,
    security_international_flights,
)
from .models import Flight, QueuePrediction
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


def _passport_security_hourly_coupling(
    flights: list,
    config: AirportConfigView,
    demand: DemandCalculator,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    now: datetime | None = None,
) -> dict:
    """
    PASSPORT → SECURITY zaman-akışlı kuplaj (bu ADIM'ın ana özelliği).

    NEDEN GEREKLİ: `security_flights()`/`security_international_flights()`
    uluslararası kalkış talebini KENDİ `effective_time()`'ında (kalkıştan
    önceki dinamik security buffer anı) security kuyruğuna sayardı - bu,
    passport'u HİÇ ATLAMADAN, passport ile security'nin AYNI ANDA/BAĞIMSIZ
    çalıştığı YANLIŞ bir varsayımdı. Gerçekte bu yolcu ÖNCE passport'a
    girer, security'ye ancak passport'tan SERBEST BIRAKILDIKTAN SONRA
    ulaşır.

    ZAMANLAMA - YENİ bir varsayım İCAT EDİLMEDİ: uluslararası kalkış
    uçuşunun MEVCUT `effective_time(f)`'ı (değişmedi) hâlâ "bu yolcu
    passport kuyruğuna GİRER" anıdır - sistemde bu yolcu için tanımlı
    TEK zaman damgası zaten bu. Uluslararası varış (`effective_time` =
    varış + sabit 15dk buffer, DEĞİŞMEDİ) SADECE passport'a girer,
    security'ye hiç GİRMEZ (yolculuk burada bitiyor). Domestic kalkış
    (`effective_time` = kalkış - dinamik security buffer, DEĞİŞMEDİ)
    passport'u ATLAYIP DOĞRUDAN security'ye girer.

    NEDEN SAATLİK, DAKİKA-ÇÖZÜNÜRLÜKLÜ DEĞİL: ilk denemede bağımsız bir
    dakika-simülasyonu kuruldu, ama bu simülasyon passport'un KENDİ
    RESMİ saatlik backlog zincirinden (`_queue_backlog_starts` +
    `passport_queue_model`, DEĞİŞTİRİLMEDİ) SESSİZCE SAPIYORDU: saatlik
    model bir saatin TÜM kapasitesini o saatin BAŞINDAN itibaren
    kullanılabilir sayarken (`service_capacity = capacity_rate *
    window_minutes`, DEĞİŞMEDİ), dakikalık model talebin GERÇEK varış
    dakikasından SONRAKİ kalan dakikalarla sınırlıyordu - aynı kuyruk
    için iki FARKLI backlog sonucu (passport'un kendi grafiğinde
    gösterilenle security'nin girdisi TUTARSIZ olurdu). Bu SAPMAYI
    önlemek için: passport'un RESMİ saatlik backlog_start/backlog_end'i
    (aşağıda `_queue_backlog_starts` ile AYNEN yeniden hesaplanır, hiç
    DEĞİŞTİRİLMEDEN) OTORİTER kabul edilir; SADECE o saat içinde
    SERBEST BIRAKILAN TOPLAMIN kalkış-bağlantılı/varış-bağlantılı PAYI
    saatlik kompozisyon oranıyla türetilir - bir saatin TOPLAM talebi/
    backlog'u için granülarite matematiksel olarak fark ETMEZ (doğrusal
    kapasite/talep muhasebesi); SADECE "bu saatte NE KADARI kalkış-
    bağlantılıydı" sorusu bu oranla cevaplanır.

    MODELLEME KARARI (açıkça belgelendi, gizlenmiyor): passport kuyruğu
    aynı anda iki "kaderde" yolcu taşıyabilir - kalkış-bağlantılı
    (security'ye devam edecek) ve varış-bağlantılı (burada biter).
    Fiziksel gişeler bu ikisi arasında ayrım YAPMAZ (aynı havuzdan
    servis eder); yolcu bazında GERÇEK bir FIFO sırası veride YOK
    (Flight tablosu tek-bacaklı, yolcu kimliği taşımıyor). Bu yüzden
    her saat serbest bırakılan TOPLAM kişi sayısı, o saatin kalkış/
    varış KOMPOZİSYON PAYINA orantılı bölünür - conservation (giren =
    işlenen + kalan kuyruk) HER saat kesindir (bkz. testler).

    `PROCESS_SECURITY_DOMESTIC` bu fonksiyondan HİÇ ETKİLENMEZ (domestic
    kalkış zaten passport'u atlıyor - eski, bağımsız/kendi tam
    kapasiteli hesap yolu AYNEN korunuyor, bkz. `predict_airport`).
    `PROCESS_SECURITY` (birleşik) ve `PROCESS_SECURITY_INTL` KENDİ
    BAĞIMSIZ/tam kapasiteli (480 pax/saat) kuyruğuna sahip AYRI simüle
    edilir - mevcut domestic/international split mimarisiyle TUTARLI;
    bu ADIM'da paylaşımlı/havuzlanmış TEK bir security kapasitesi
    modeline GEÇİLMEDİ (kapsam dışı, YASAKLAR'da "security 8 lane'i
    değiştirme").

    now : security'nin "an itibariyle" (current) kısmi serbest bırakma
          toplamı için (bkz. dönen `current_released_by_hour`) -
          `_predict_window_core`'un `current_arrived_demand`
          mantığıyla AYNI "sadece şimdiye kadar gerçekleşen" ilkesi;
          KAPALI (window_end<=now) saatler TAM değeri, AÇIK (şu anki)
          saat passport'un o saat içindeki geçen-süre ORANINI, GELECEK
          saatler 0 taşır - bu da YENİ bir zamanlama varsayımı değil,
          passport'un KENDİ `elapsed_minutes` oranının security'nin
          devraldığı paya uygulanmasıdır.

    Dönen sözlük: `demand_by_hour[process][hour]`,
    `backlog_start_by_hour[process][hour]`,
    `current_released_by_hour[process][hour]` (process = PROCESS_SECURITY
    veya PROCESS_SECURITY_INTL).
    """
    from .domain.flows import (
        is_international_departure,
        is_international_arrival,
        passport_flights,
        security_domestic_flights,
    )

    _now = now if now is not None else domain_now()

    passport_bucket = _bucket_flights_by_window(passport_flights(flights), window_minutes)
    domestic_bucket = _bucket_flights_by_window(
        security_domestic_flights(flights), window_minutes
    )

    empty_result = {
        "demand_by_hour": {PROCESS_SECURITY: {}, PROCESS_SECURITY_INTL: {}},
        "backlog_start_by_hour": {PROCESS_SECURITY: {}, PROCESS_SECURITY_INTL: {}},
        "current_released_by_hour": {PROCESS_SECURITY: {}, PROCESS_SECURITY_INTL: {}},
    }
    if not passport_bucket and not domestic_bucket:
        return empty_result

    passport_rate = passport_capacity_rate(config)
    security_rate = security_capacity_rate(config)

    security_intl_demand_by_hour: dict[datetime, float] = {}

    if passport_bucket:
        # 1) Passport'un RESMİ saatlik backlog RECURRENCE'ı (DEĞİŞMEDİ -
        #    `_queue_backlog_starts`'ın KULLANDIĞI AYNI formül:
        #    `backlog_end = max(0, backlog_start + demand - service_capacity)`)
        #    - BURADA, GAP saatler (13:00/14:00 gibi hiç uçuşu olmayan ama
        #    ilk/son dolu saat arasında kalan saatler) DAHİL, HER saat
        #    için KENDİ İÇİNDE yeniden hesaplanır. `_queue_backlog_starts`'ın
        #    KENDİSİ ÇAĞRILMADI: o fonksiyon gap saatleri de dahili olarak
        #    doğru şekilde boşaltıyor ama DÖNDÜRDÜĞÜ sözlük SADECE dolu
        #    saatler için anahtar taşıyor - gap saatlerinde `.get(hour,
        #    0.0)` YANLIŞ biçimde 0'a düşüp kalkış-bağlantılı backlog'u
        #    gap saatlerinde SESSİZCE SIFIRLARDI (bu, ilk denemede
        #    yakalanan gerçek bir hata). Bu yüzden official backlog burada
        #    HER saat (gap dahil) için KENDİ döngüsünde takip edilir -
        #    formülün KENDİSİ `_queue_backlog_starts` ile BİREBİR AYNI.
        service_capacity = passport_rate * window_minutes
        dep_backlog = 0.0
        arr_backlog = 0.0
        official_backlog = 0.0
        starts = sorted(passport_bucket)
        current = starts[0]
        last = starts[-1]
        step = timedelta(minutes=window_minutes)
        # Görev madde 7 - "08:59'da passport'tan çıkan yolcu security
        # açısından kaybolmamalı": son GERÇEK uçuş saatinden SONRA da,
        # backlog (dep_backlog+arr_backlog, `official_backlog` ile AYNI
        # toplam) tamamen boşalana kadar dolaşmaya DEVAM edilir - aksi
        # halde `last`'ten sonraki saatlerde hâlâ serbest bırakılacak
        # kalkış-bağlantılı yolcular security'ye HİÇ ulaşmadan "kaybolur".
        #
        # SINIRLI ufuk (24 saat/`last`'ten sonra) - BİLİNÇLİ bir sınır:
        # passport'un KENDİ resmi backlog zinciri (`_queue_backlog_starts`,
        # DEĞİŞMEDİ) de `last`'ten SONRA hiç devam ETMEZ - son gerçek
        # uçuş saatinden sonraki kalıntı hiçbir zaman gösterilmez (mevcut,
        # onaylanmış bir sınırlama). Gerçek-ölçekli bir operasyonel
        # veri kümesinde (bkz. rapor - passport kapasitesi saatte 320
        # yolcu, gerçekçi bir günün toplam uluslararası talebinin
        # ÇOK altında kalabiliyor) bu SINIR OLMADAN backlog günler/
        # haftalarca "boşalmaya devam eder" gibi görünüp saatlik
        # grafiklerde anlamsız, çok uzak gelecek pencereleri üretirdi.
        # 24 saatlik ufuk, yakın saat/gün sınırı aktarımını (görevin
        # istediği asıl senaryo) doğru şekilde yakalar, ama passport'un
        # KENDİ sınırlamasıyla TUTARLI kalarak sınırsız ileri sürüklenmeyi
        # önler. Bu ufkun ötesinde kalan kalıntı security'ye YANSIMAZ -
        # AÇIKÇA belgelenen bir sınırlama (bkz. rapor), sessizce gizlenmiyor.
        horizon = last + timedelta(minutes=window_minutes * 24)

        while current <= last or (official_backlog > 1e-9 and current <= horizon):
            window_all = passport_bucket.get(current, [])
            window_flights = [f for f in window_all if f.status not in EXCLUDED_STATUSES]
            demand_dep = sum(
                demand.passenger_demand(f) for f in window_flights
                if is_international_departure(f)
            )
            demand_arr = sum(
                demand.passenger_demand(f) for f in window_flights
                if is_international_arrival(f)
            )
            demand_total = demand_dep + demand_arr

            backlog_start_official = official_backlog
            backlog_end_official = max(
                0.0, backlog_start_official + demand_total - service_capacity
            )
            official_backlog = backlog_end_official
            served_total = backlog_start_official + demand_total - backlog_end_official

            pre_dep = dep_backlog + demand_dep
            pre_arr = arr_backlog + demand_arr
            pre_total = pre_dep + pre_arr
            served_dep = served_total * (pre_dep / pre_total) if pre_total > 0 else 0.0
            served_arr = served_total - served_dep

            dep_backlog = max(0.0, pre_dep - served_dep)
            arr_backlog = max(0.0, pre_arr - served_arr)
            # served_arr (varış-bağlantılı) burada KAYBOLUYOR - yolculuk
            # bitti, hiçbir kuyruğa AKTARILMIYOR (double-count YOK).

            if served_dep > 0 or current in security_intl_demand_by_hour:
                security_intl_demand_by_hour[current] = (
                    security_intl_demand_by_hour.get(current, 0.0) + served_dep
                )

            current += step

    # 2) Domestic kalkış - passport'u ATLAYIP doğrudan security'ye
    #    (kendi RAW effective_time'ında, DEĞİŞMEDİ).
    direct_demand_by_hour: dict[datetime, float] = {}
    for hour, window_all in domestic_bucket.items():
        window_flights = [f for f in window_all if f.status not in EXCLUDED_STATUSES]
        total = sum(demand.passenger_demand(f) for f in window_flights)
        if total > 0:
            direct_demand_by_hour[hour] = total

    combined_demand_by_hour: dict[datetime, float] = {}
    for hour in set(direct_demand_by_hour) | set(security_intl_demand_by_hour):
        total = direct_demand_by_hour.get(hour, 0.0) + security_intl_demand_by_hour.get(hour, 0.0)
        if total > 0:
            combined_demand_by_hour[hour] = total

    demand_by_hour = {
        PROCESS_SECURITY: combined_demand_by_hour,
        PROCESS_SECURITY_INTL: security_intl_demand_by_hour,
    }
    backlog_start_by_hour = {
        PROCESS_SECURITY: _hourly_backlog_chain(
            combined_demand_by_hour, security_rate, window_minutes
        ),
        PROCESS_SECURITY_INTL: _hourly_backlog_chain(
            security_intl_demand_by_hour, security_rate, window_minutes
        ),
    }

    # 3) "An itibariyle" (current) kısmi pay - KAPALI saatler TAM değeri,
    #    AÇIK (şu anki) saat elapsed-oranını, GELECEK saatler 0 taşır.
    current_released_by_hour = {PROCESS_SECURITY: {}, PROCESS_SECURITY_INTL: {}}
    for process, by_hour in demand_by_hour.items():
        for hour, value in by_hour.items():
            hour_end = hour + timedelta(minutes=window_minutes)
            if hour_end <= _now:
                current_released_by_hour[process][hour] = value
            elif hour <= _now < hour_end:
                fraction = max(0.0, min(
                    (_now - hour).total_seconds() / 60.0, window_minutes
                )) / window_minutes
                current_released_by_hour[process][hour] = value * fraction
            # gelecekteki saat -> hiç girilmiyor (0.0 varsayılan)

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
    try:
        passport_rate = passport_capacity_rate(config)
        security_rate = security_capacity_rate(config)
    except ValueError as exc:
        raise ValueError(f"{airport_iata}: geçersiz queue config - {exc}") from exc

    if aircraft_match_rate is None:
        aircraft_match_rate = flight_match_rate(flights)

    predictions: list[WindowPrediction] = []

    # PASSPORT→SECURITY zaman-kuplajı: PROCESS_SECURITY (birleşik) ve
    # PROCESS_SECURITY_INTL için passport'un RESMİ saatlik backlog
    # zincirinden türetilen saatlik kuplaj (bkz. fonksiyon docstring'i).
    # PROCESS_PASSPORT ve PROCESS_SECURITY_DOMESTIC bundan ETKİLENMEZ
    # (aşağıdaki eski/değişmemiş yol üzerinden hesaplanmaya devam eder).
    coupling = _passport_security_hourly_coupling(
        flights, config, demand, window_minutes=window_minutes, now=now
    )

    for process in PROCESSES:
        relevant = _PROCESS_FLIGHTS[process](flights)
        buckets = _bucket_flights_by_window(relevant, window_minutes)

        if process in (PROCESS_SECURITY, PROCESS_SECURITY_INTL):
            coupled_demand = coupling["demand_by_hour"][process]
            coupled_backlog = coupling["backlog_start_by_hour"][process]
            coupled_current = coupling["current_released_by_hour"][process]
            # window_starts = RAW uçuş pencereleri (flight_count/neden
            # tespiti için) BİRLEŞİMİ simülasyonun ürettiği saatlerle
            # (backlog boşalma saatleri RAW uçuş içermeyebilir - ör.
            # passport geç serbest bıraktığı için security'ye ancak bir
            # SONRAKİ saatte ulaşan yolcular).
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
                ))
            continue

        # PROCESS_PASSPORT / PROCESS_SECURITY_DOMESTIC - DEĞİŞMEDİ.
        capacity_rate = (
            passport_rate if process == PROCESS_PASSPORT else security_rate
        )
        backlog_starts = _queue_backlog_starts(
            buckets, capacity_rate, demand.passenger_demand, window_minutes
        )

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
                backlog_start=backlog_starts.get(start, 0.0),
                aircraft_changes=aircraft_changes,
                window_minutes=window_minutes,
                historical_passenger_baseline=passenger_baseline,
                now=now,
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

    # ADIM 6D-2: passport "an itibariyle" (current) bekleme hesabı
    # (predict_airport) VE açık/kapalı pencere kararı (record_baseline_
    # observations) AYNI "now" anını kullanmalı - burada BİR KEZ çözülüp
    # her ikisine de aynen geçilir.
    resolved_now = now if now is not None else domain_now()

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
        **written,
    }
