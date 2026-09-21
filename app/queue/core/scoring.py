"""
AŞAMA 5 + AŞAMA 7 - Risk skorlama ve güven skoru.

SAF KATMAN: Bu modül veritabanına, dosyaya veya ağa dokunmaz.
Yolcu talebi bir fonksiyon olarak (demand_fn) dışarıdan enjekte
edilir; böylece mock verilerle bağımsız test edilebilir.

Security ve passport aynı doğrulanmış queue-capacity çekirdeğini kullanır;
fiziksel server sayısı ve servis süresi process config'inden gelir. Security
density oranları yalnız tarihsel açıklama/baseline metriği olarak korunur,
queue risk/wait matematiğinin yerine geçmez.
"""

from typing import Callable, Sequence

from .erlang import erlang_c_wait_time
from ..constants import (
    AIRCRAFT_MATCH_RATE_THRESHOLD,
    CONFIDENCE_FLOOR,
    CONFIDENCE_PENALTY_BOARDING_BUFFER,
    CONFIDENCE_PENALTY_DEFAULT_CONFIG,
    CONFIDENCE_PENALTY_LOAD_FACTOR,
    CONFIDENCE_PENALTY_LOW_MATCH_RATE,
    CONFIDENCE_PENALTY_NO_BASELINE,
    CONFIDENCE_PENALTY_NULL_AIRCRAFT,
    DEMAND_WINDOW_MINUTES,
    NO_BASELINE_MESSAGE,
    PASSPORT_OVERLOAD_MESSAGE,
    PASSPORT_RHO_LOW,
    PASSPORT_RHO_MEDIUM,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    SECURITY_FLIGHT_RATIO_WEIGHT,
    SECURITY_PASSENGER_RATIO_WEIGHT,
    SECURITY_RATIO_HIGH,
    SECURITY_RATIO_LOW,
    SECURITY_RATIO_MEDIUM,
)


def combined_security_ratio(
    flight_ratio: float, passenger_ratio: float | None
) -> float:
    """
    MADDE 7 - iki sinyalin ağırlıklı ortalaması.

        combined = (Wf * flight_ratio + Wp * passenger_ratio) / (Wf + Wp)

    Ağırlıklar toplamlarına bölünerek normalize edilir; böylece
    birleşik oran tek tek oranlarla AYNI ölçekte kalır ve mevcut risk
    bantları (1.2 / 1.4 / 1.8) değişmeden geçerli olur.

    passenger_ratio None ise (geçmiş yolcu verisi yok) yalnızca
    flight_ratio kullanılır - eksik sinyal 0 sayılıp riski YAPAY olarak
    aşağı çekmez, uydurma bir değerle de doldurulmaz. Bu, yolcu
    baseline'ı birikene kadar eski davranışın birebir korunması
    demektir.
    """
    if passenger_ratio is None:
        return flight_ratio

    total_weight = (
        SECURITY_FLIGHT_RATIO_WEIGHT + SECURITY_PASSENGER_RATIO_WEIGHT
    )
    if total_weight <= 0:
        return flight_ratio

    return (
        SECURITY_FLIGHT_RATIO_WEIGHT * flight_ratio
        + SECURITY_PASSENGER_RATIO_WEIGHT * passenger_ratio
    ) / total_weight


def security_density_score(
    window_flights: Sequence,
    historical_baseline: float | None,
    demand_fn: Callable[[object], int],
    historical_passenger_baseline: float | None = None,
) -> dict:
    """
    Security tarihsel yoğunluk sinyali. Queue risk/wait hesabı artık
    `security_queue_model` ile yapılır; bu fonksiyon yalnız API'deki
    baseline/flight/passenger ratio tanı metriklerini üretir.

    window_flights : domestic departure + international departure
    historical_baseline
                   : bu havalimanı + saat için geçmiş ortalama UÇUŞ
                     sayısı. Yoksa None - SAHTE DEĞER ÜRETİLMEZ.
    historical_passenger_baseline
                   : aynı pencere için geçmiş ortalama YOLCU talebi.
                     Yoksa None -> passenger_ratio None kalır ve
                     birleşik orana hiç katılmaz (MADDE 7).

    MADDE 7: risk yalnızca uçuş sayısına göre değil, uçuş oranı ile
    yolcu oranının ağırlıklı ortalamasına göre belirlenir. Böylece
      - az uçuş + geniş gövde  -> passenger_ratio yükselir, risk artar
      - çok uçuş + küçük uçak  -> flight_ratio yükselir, risk artar
    ikisi de ayrı ayrı görünür olur.

    estimated_wait_minutes burada None kalır; çağıran motor gerçek
    lane/service-time config'iyle queue modelinden gelen değeri kullanır.
    """
    flight_count = len(window_flights)
    demand = sum(demand_fn(f) for f in window_flights)

    if not historical_baseline:
        return {
            "flight_count": flight_count,
            "expected_passengers": demand,
            "baseline_ratio": None,
            "flight_ratio": None,
            "passenger_ratio": None,
            "risk": RISK_UNKNOWN,
            "estimated_wait_minutes": None,
            "reasons": [NO_BASELINE_MESSAGE],
        }

    flight_ratio = flight_count / historical_baseline

    # Sıfır/None yolcu baseline'ı ile bölme YOK - böyle bir durumda
    # oran hesaplanmaz, None kalır.
    passenger_ratio = (
        demand / historical_passenger_baseline
        if historical_passenger_baseline else None
    )

    ratio = combined_security_ratio(flight_ratio, passenger_ratio)

    if ratio < SECURITY_RATIO_LOW:
        risk = RISK_LOW
    elif ratio < SECURITY_RATIO_MEDIUM:
        risk = RISK_MEDIUM
    elif ratio < SECURITY_RATIO_HIGH:
        risk = RISK_HIGH
    else:
        risk = RISK_CRITICAL

    return {
        "flight_count": flight_count,
        "expected_passengers": demand,
        "baseline_ratio": round(ratio, 2),
        "flight_ratio": round(flight_ratio, 2),
        "passenger_ratio": (
            round(passenger_ratio, 2) if passenger_ratio is not None else None
        ),
        "risk": risk,
        "estimated_wait_minutes": None,
        "reasons": [],   # AŞAMA 6 dolduracak
    }


def passport_effective_service_rate(config) -> float:
    """
    Passport TEK bir görevlinin servis hızı (mu, pax/dk/görevli).

    mu = 1 / 1.5 = 0.6666667. Bu SUNUCU BAŞINA (bir görevli) hızdır -
    kaç paralel görevli olduğu (`passport_effective_server_count`) AYRI
    bir çarpandır, burada karışmaz.
    """
    service_time = config.passport_service_time_minutes
    if service_time <= 0:
        raise ValueError(
            "passport_service_time_minutes > 0 olmalı "
            f"(alınan={service_time})"
        )
    return 1.0 / service_time


def passport_effective_server_count(config) -> int:
    """
    Erlang-C'nin `c` parametresi: PARALEL çalışan görevli sayısı.

    4 gişe × gişe başına 2 paralel görevli = 8 efektif server. Gişe
    (`passport_counter_count`) fiziksel masa/kabin sayısıdır; her
    gişede AYNI ANDA `passport_staff_per_counter` görevli AYRI birer
    yolcu işleyebiliyorsa (bu ADIM'ın açık modelleme kararı), Erlang-C
    kuyruk teorisindeki "server" gişe DEĞİL, görevlidir - bu yüzden
    c = counter_count * staff_per_counter'dır, counter_count TEK
    BAŞINA değil. `passport_staff_count` (toplam personel, vardiya/
    rotasyon bilgisi) buraya KARIŞMAZ - o ayrı, bilgilendirici bir
    alandır (bkz. `passport_staff_count_mismatch`).

    `int`'e yuvarlanır: Erlang-C'nin `c!`/`range(c)` kullanan
    kombinatorik formülü (bkz. `core/erlang.py`) YAPISAL OLARAK tam
    sayı gerektirir - "7.5 paralel görevli" fiziksel olarak anlamsız,
    kesirli bir server SAYISI matematiksel olarak tanımsızdır (kesirli
    olan sadece servis HIZI/mu'dur). 4x2=8 gibi tam sayı veren
    varsayılan config için bu yuvarlama hiçbir şeyi DEĞİŞTİRMEZ.
    """
    counters = config.passport_counter_count
    staff_per_counter = config.passport_staff_per_counter
    if counters <= 0:
        raise ValueError(f"passport_counter_count > 0 olmalı (alınan={counters})")
    if staff_per_counter <= 0:
        raise ValueError(
            f"passport_staff_per_counter > 0 olmalı (alınan={staff_per_counter})"
        )
    return round(counters * staff_per_counter)


def passport_server_count(config, pool: str) -> int:
    """
    ADIM (Airport-Scale Queue Capacity) - departure/arrival passport
    ARTIK AYRI fiziksel havuz (bkz. core/event_queue.py); `config`'in
    ZATEN çözülmüş (`app/queue/config.py`'nin override->scale->unknown
    zincirinden geçmiş) `passport_departure_server_count`/`passport_
    arrival_server_count` alanlarını okur - burada scale/hard-code YOK,
    sadece `config`'i tüketir.

    `pool` : "departure" veya "arrival".
    """
    if pool == "departure":
        count = config.passport_departure_server_count
    elif pool == "arrival":
        count = config.passport_arrival_server_count
    else:
        raise ValueError(f'pool "departure" veya "arrival" olmalı (alınan={pool!r})')
    if count <= 0:
        raise ValueError(
            f"passport_{pool}_server_count > 0 olmalı (alınan={count})"
        )
    return count


def passport_departure_server_count(config) -> int:
    return passport_server_count(config, "departure")


def passport_arrival_server_count(config) -> int:
    return passport_server_count(config, "arrival")


def security_effective_service_rate(config) -> float:
    """Security lane başına servis hızı: 1 / service_time (pax/dk/lane)."""
    service_time = config.security_service_time_minutes
    if service_time <= 0:
        raise ValueError(
            "security_service_time_minutes > 0 olmalı "
            f"(alınan={service_time})"
        )
    return 1.0 / service_time


def queue_capacity_rate(server_count: int, service_time_minutes: float) -> float:
    """Ortak kapasite hesabı: c * mu; invalid config sessizce kabul edilmez."""
    if server_count <= 0:
        raise ValueError(f"server_count > 0 olmalı (alınan={server_count})")
    if service_time_minutes <= 0:
        raise ValueError(
            "service_time_minutes > 0 olmalı "
            f"(alınan={service_time_minutes})"
        )
    return server_count * (1.0 / service_time_minutes)


def passport_capacity_rate(config) -> float:
    """
    4 gişe x gişe başına 2 paralel görevli = 8 efektif server;
    mu = 1/1.5 = 0.6666667 pax/dk/görevli ->
    capacity_rate = 8 x 0.6666667 = 5.333333 pax/dk = 320 pax/saat.
    """
    return queue_capacity_rate(
        passport_effective_server_count(config),
        config.passport_service_time_minutes,
    )


def passport_departure_capacity_rate(config) -> float:
    """Departure passport havuzunun KENDİ referans kapasitesi (server_count x 1/service_time)."""
    return queue_capacity_rate(
        passport_departure_server_count(config),
        config.passport_service_time_minutes,
    )


def passport_arrival_capacity_rate(config) -> float:
    """Arrival passport havuzunun KENDİ referans kapasitesi - departure'dan BAĞIMSIZ."""
    return queue_capacity_rate(
        passport_arrival_server_count(config),
        config.passport_service_time_minutes,
    )


def security_capacity_rate(config) -> float:
    return queue_capacity_rate(
        config.security_lane_count,
        config.security_service_time_minutes,
    )


def domestic_security_capacity_rate(config) -> float:
    """
    `PROCESS_SECURITY_DOMESTIC`'in KENDİ fiziksel lane sayısı.

    Bu süreç passport->security kuplajından tamamen bağımsızdır (domestic
    kalkış passport'u hiç görmeden doğrudan security'ye girer) - bu
    yüzden `security_lane_count`'tan (birleşik/international'ın da
    kullandığı) AYRI, airport-bazlı bir değer güvenle kullanılabilir.
    """
    return queue_capacity_rate(
        config.domestic_security_lane_count,
        config.security_service_time_minutes,
    )


def international_security_capacity_rate(config) -> float:
    """
    `PROCESS_SECURITY_INTL`'in KENDİ fiziksel lane sayısı
    (`international_security_lane_count`) - artık gerçek event-driven
    international security hesabında (bkz. engine.py
    `_event_driven_queue_demand`) KULLANILIYOR.
    """
    return queue_capacity_rate(
        config.international_security_lane_count,
        config.security_service_time_minutes,
    )


def passport_staff_count_mismatch(config) -> bool:
    """
    passport_staff_count, counter_count * staff_per_counter ile
    uyuşmuyor mu?

    Sadece bilgilendirme amaçlıdır - kapasite hesabına (rho, mu)
    hiçbir şekilde girmez; staff_count orada zaten kullanılmıyor.
    """
    expected = config.passport_counter_count * config.passport_staff_per_counter
    return config.passport_staff_count != expected


def queue_capacity_model(
    window_flights: Sequence,
    demand_fn: Callable[[object], int],
    server_count: int,
    service_time_minutes: float,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    backlog_start: float = 0.0,
    current_arrived_demand: float | None = None,
    elapsed_minutes: float | None = None,
    demand_override: float | None = None,
    risk_backlog_start: float = 0.0,
) -> dict:
    """
    Passport ve security için TEK queue matematik implementasyonu.

    - Stable (`backlog_start<=0`, `rho<1`): klasik Erlang-C.
    - Overload/backlog: current-arrived demand ve geçen servis süresinden
      gerçek anlık kuyruk/wait.
    - Backlog recurrence her zaman TAM pencere demand'ini kullanır.

    demand_override : PASSPORT→SECURITY zaman-kuplajı için (bkz.
                       `engine.py:_passport_security_minute_simulation`).
                       Verilirse `demand`, `window_flights` üzerinden
                       TOPLANMAZ - doğrudan bu değer kullanılır (ör.
                       security'nin bu pencerede GERÇEKTEN karşılaştığı,
                       passport'tan zaman-kaydırmalı serbest bırakılmış
                       yolcu sayısı). `window_flights` bu durumda SADECE
                       `flight_count`/neden tespiti için kullanılmaya
                       devam eder - AŞAĞIDAKİ formülün (lam/rho/backlog/
                       wait) KENDİSİ HİÇ DEĞİŞMEDİ, sadece demand'in
                       KAYNAĞI değişti. Verilmezse (None) eski davranış
                       birebir korunur.

    risk_backlog_start : ADIM (Visible Risk = Gerçek Queue Pressure) -
                       `backlog_start` (yukarıdaki, WAIT hesabını besleyen,
                       fluid/analitik backlog) İLE KARIŞTIRILMAMALI: bu,
                       SADECE `risk` sınıflandırması için kullanılan,
                       GERÇEK/event-türevli ("T anında henüz servise
                       başlamamış, önceki pencerelerden taşınan gerçek
                       bekleyen yolcu sayısı" - bkz. `engine.py:
                       _event_derived_backlog_by_hour()`) backlog'dur.
                       `rho` (= incoming-only utilization, `utilization`
                       API alanında GERİYE DÖNÜK UYUMLU olarak AYNEN
                       kalır) BU parametreden HİÇ ETKİLENMEZ - SADECE
                       yeni `queue_pressure` (risk'in TEK girdisi) bunu
                       kullanır. Verilmezse (varsayılan 0.0) `queue_
                       pressure == rho` olur - ESKİ risk davranışı
                       BİREBİR korunur (legacy `PROCESS_PASSPORT`/
                       `PROCESS_SECURITY` ve doğrudan `predict_window()`
                       çağıranları dahil - hiçbiri bu parametreyi
                       VERMEZ, dolayısıyla ETKİLENMEZ).
    """
    demand = (
        sum(demand_fn(f) for f in window_flights)
        if demand_override is None else demand_override
    )
    lam = demand / window_minutes
    c = server_count
    mu = 1.0 / service_time_minutes if service_time_minutes > 0 else 0.0
    capacity_rate = queue_capacity_rate(c, service_time_minutes)
    rho = lam / capacity_rate

    service_capacity = capacity_rate * window_minutes
    backlog_end = max(0.0, backlog_start + demand - service_capacity)

    # ADIM (Visible Risk = Gerçek Queue Pressure) - risk ARTIK sadece bu
    # pencerenin KENDİ gelen talebine (`rho`) değil, GERÇEK, event-türevli
    # bekleyen backlog'a da bakar: "bu saat sunucuların temizlemesi
    # gereken TOPLAM yük (eski bekleyen + yeni gelen) / toplam kapasite".
    # Birim kontrolü: (kişi + kişi) / kişi = boyutsuz - AYNI `rho` ile
    # AYNI ölçek/eşik bantlarını (0.7/0.9/1.0) kullanabilir. `rho`'nun
    # KENDİSİ (incoming-only) `utilization` alanında DEĞİŞMEDEN kalır
    # (Bölüm 6 - API geriye-uyumluluğu, diagnostic/internal değer).
    queue_pressure = (demand + risk_backlog_start) / service_capacity

    if queue_pressure >= 1.0:
        risk = RISK_CRITICAL
        reasons = [PASSPORT_OVERLOAD_MESSAGE]
    elif queue_pressure < PASSPORT_RHO_LOW:
        risk = RISK_LOW
        reasons = []
    elif queue_pressure < PASSPORT_RHO_MEDIUM:
        risk = RISK_MEDIUM
        reasons = []
    else:
        risk = RISK_HIGH
        reasons = []

    if backlog_start <= 0.0 and rho < 1.0:
        wq = erlang_c_wait_time(c, lam, mu)
    else:
        arrived = demand if current_arrived_demand is None else current_arrived_demand
        elapsed = window_minutes if elapsed_minutes is None else elapsed_minutes
        elapsed = max(0.0, min(elapsed, window_minutes))
        served_since_window_start = capacity_rate * elapsed
        current_queue = max(0.0, backlog_start + arrived - served_since_window_start)
        wq = current_queue / capacity_rate

    return {
        "flight_count": len(window_flights),
        "expected_passengers": demand,
        "arrival_rate": round(lam, 3),
        "server_count": c,
        "service_rate": round(mu, 6),
        "capacity_rate": round(capacity_rate, 6),
        "utilization": round(rho, 3),
        "queue_pressure": round(queue_pressure, 3),
        "estimated_wait_minutes": round(wq, 1),
        "backlog_end": round(backlog_end, 3),
        "risk": risk,
        "reasons": reasons,
    }


def passport_queue_model(
    window_flights: Sequence,
    config,
    demand_fn: Callable[[object], int],
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    backlog_start: float = 0.0,
    current_arrived_demand: float | None = None,
    elapsed_minutes: float | None = None,
    demand_override: float | None = None,
    pool: str | None = None,
    risk_backlog_start: float = 0.0,
) -> dict:
    """
    Passport wrapper.

    pool : ADIM (Airport-Scale Queue Capacity) - `None` ise (varsayılan,
           geriye dönük uyumlu) ESKİ birleşik `passport_effective_
           server_count()` (4 gişe x 2 görevli) kullanılır - legacy
           `PROCESS_PASSPORT` (combined) satırı için hâlâ geçerli.
           "departure"/"arrival" verilirse `passport_server_count(config,
           pool)` - AYRI fiziksel havuzun KENDİ server sayısı - kullanılır
           (bkz. `engine.py`'nin PROCESS_PASSPORT_DEPARTURE/ARRIVAL
           hesabı).
    demand_override : security_queue_model() ile AYNI amaç - departure/
           arrival event-driven demand'i `window_flights`'tan YENİDEN
           TOPLAMAK yerine doğrudan kullanmak için (bkz. Bölüm 6'daki
           bulgu: eski kodda PASSPORT için bu parametre HİÇ
           kullanılmıyordu - artık departure/arrival AYRI havuzlar
           event-driven demand'e ihtiyaç duyuyor).
    """
    server_count = (
        passport_effective_server_count(config) if pool is None
        else passport_server_count(config, pool)
    )
    return queue_capacity_model(
        window_flights=window_flights,
        demand_fn=demand_fn,
        server_count=server_count,
        service_time_minutes=config.passport_service_time_minutes,
        window_minutes=window_minutes,
        backlog_start=backlog_start,
        current_arrived_demand=current_arrived_demand,
        elapsed_minutes=elapsed_minutes,
        demand_override=demand_override,
        risk_backlog_start=risk_backlog_start,
    )


def security_queue_model(
    window_flights: Sequence,
    config,
    demand_fn: Callable[[object], int],
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    backlog_start: float = 0.0,
    current_arrived_demand: float | None = None,
    elapsed_minutes: float | None = None,
    demand_override: float | None = None,
    lane_count_override: int | None = None,
    risk_backlog_start: float = 0.0,
) -> dict:
    """
    Security wrapper: c=lane_count, service time config'ten.

    lane_count_override : ADIM (Domestic/International Security Lane
                           Ayrımı) - verilirse `config.security_lane_count`
                           yerine bu değer `c` olarak kullanılır (ör.
                           `PROCESS_SECURITY_DOMESTIC` için
                           `config.domestic_security_lane_count`).
                           Verilmezse (None) eski davranış birebir
                           korunur - birleşik/`PROCESS_SECURITY_INTL`
                           çağıranları ETKİLENMEZ.
    """
    return queue_capacity_model(
        window_flights=window_flights,
        demand_fn=demand_fn,
        server_count=(
            config.security_lane_count
            if lane_count_override is None else lane_count_override
        ),
        service_time_minutes=config.security_service_time_minutes,
        window_minutes=window_minutes,
        backlog_start=backlog_start,
        current_arrived_demand=current_arrived_demand,
        elapsed_minutes=elapsed_minutes,
        demand_override=demand_override,
        risk_backlog_start=risk_backlog_start,
    )


def confidence_score(
    window_flights: Sequence,
    historical_baseline_available: bool,
    aircraft_match_rate: float,
    config_is_default: bool,
) -> float:
    """
    AŞAMA 7 - Tahminin ne kadar sağlam veriye dayandığı.

    Load factor ve boarding buffer HER ZAMAN statik varsayım olduğu
    için bu iki ceza koşulsuz uygulanır.
    """
    score = 1.0

    score -= CONFIDENCE_PENALTY_LOAD_FACTOR
    score -= CONFIDENCE_PENALTY_BOARDING_BUFFER

    if aircraft_match_rate < AIRCRAFT_MATCH_RATE_THRESHOLD:
        score -= CONFIDENCE_PENALTY_LOW_MATCH_RATE

    if any(getattr(f, "aircraft_icao", None) is None for f in window_flights):
        score -= CONFIDENCE_PENALTY_NULL_AIRCRAFT

    if not historical_baseline_available:
        score -= CONFIDENCE_PENALTY_NO_BASELINE

    if config_is_default:
        score -= CONFIDENCE_PENALTY_DEFAULT_CONFIG

    return max(round(score, 2), CONFIDENCE_FLOOR)
