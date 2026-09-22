"""
ADIM (Event-Driven Queue Core) - gerçek arrival/service-start/completion
zaman damgalarıyla çalışan, saatlik bucket toplamına DAYANMAYAN kuyruk
simülasyon çekirdeği.

SAF KATMAN: `core/scoring.py` gibi veritabanına/dosyaya/ağa dokunmaz.
`core/scoring.py`'yi BOZMAZ/DEĞİŞTİRMEZ - Erlang-C/capacity-rate
fonksiyonları (`core/erlang.py`, `core/scoring.py`) SADECE referans/
doğrulama için kullanılabilir (bkz. testler), bu modülün KENDİ
matematiği onlara bağımlı DEĞİLDİR.

NEDEN AYRI BİR MODÜL: mevcut `queue_capacity_model()` (core/scoring.py)
saatlik toplam demand üzerinden Erlang-C/backlog-recurrence hesaplar -
bu, GRAFİK/API için doğru ve YETERLİ bir referans modeldir, ama gerçek
"bu passenger grubu tam olarak HANGİ saniyede servise başladı/bitti"
sorusuna cevap veremez (saatlik agregasyon bunu YOK EDER). Bu modül
TAM TERSİ yönden çalışır: gerçek passenger arrival event'lerinden
(exact timestamp) başlayıp, c sunucunun (heapq ile tutulan) bir sonraki
müsait zamanına göre servis başlangıcını/bitişini üretir - matematiksel
olarak tek-tek passenger service mantığıyla EŞDEĞERDİR (bkz. testler),
ama hiçbir Passenger ORM satırı YARATMAZ; her "cohort" (aynı kaynaktan,
aynı anda gelen N kişi) TEK bir (origin, count) birimi olarak kuyruğa
girer, sadece SERVİS ANINDA teker teker sunuculara dağıtılır (O(N log c),
bkz. AŞAMA 41).

ENTEGRASYON: bu modül `engine.py:_event_driven_queue_demand()` tarafından
KULLANILIYOR (Event-Driven Engine Entegrasyonu ADIM'ından beri) - saatlik
graph/API contract'ı bu modülün ÇIKTISINDAN türetilir (bkz. `floor_to_
window()`'un SADECE raporlama aşamasında uygulanması).

ADIM (Airport-Scale Queue Capacity): `simulate_passport()` artık TEK bir
ORTAK havuz DEĞİL - departure ve arrival için AYRI, bağımsız `simulate_
fifo_queue()` çağrısı (bkz. fonksiyonun kendi docstring'i).
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence


@dataclass(frozen=True)
class ServiceEvent:
    """
    Bir kuyruktan servis alan TEK bir (origin, count) birimi.

    `count` bu olayda AYNI ANDA tamamlanan kişi sayısıdır (aynı sunucu
    dalgasında tamamlanan farklı segment'ler birleştirilmiş olabilir -
    bkz. `simulate_fifo_queue`). `arrival_time`/`service_start_time`/
    `completion_time` AŞAMA 22/23'ün istediği üç zaman damgasıdır;
    `wait_minutes` bunlardan TÜRETİLİR, ayrıca saklanmaz (tek doğruluk
    kaynağı zaman damgalarıdır).
    """

    origin: str
    count: float
    arrival_time: datetime
    service_start_time: datetime
    completion_time: datetime

    @property
    def wait_minutes(self) -> float:
        """AŞAMA 23: wait = service_start - queue arrival (dakika)."""
        return (self.service_start_time - self.arrival_time).total_seconds() / 60.0


def simulate_fifo_queue(
    arrivals: Sequence[tuple[datetime, str, float]],
    server_count: int,
    service_time_minutes: float,
) -> list[ServiceEvent]:
    """
    c sunuculu, tek servis süreli (deterministik), work-conserving FIFO
    kuyruk - gerçek discrete-event simülasyonu.

    arrivals : (timestamp, origin_etiketi, count) üçlüleri. `origin`
               serbest bir string'dir (ör. "departure"/"arrival" passport
               için, "domestic"/"international" security için) - SADECE
               çıktıda hangi kaynaktan geldiğini ayırt etmek için taşınır,
               kuyruk matematiğini ETKİLEMEZ (aynı fiziksel havuzu
               paylaşırlar - AŞAMA 17/29: double-count YOK, aynı sunucular
               iki kere yaratılmaz).

    TIE-BREAK (açıkça belirtilir, gizlenmez): aynı `timestamp`'te birden
    fazla `arrivals` girdisi varsa, kuyruğa girme sırası `arrivals`
    listesindeki SIRAYLA aynıdır (stabil sort). Kaynakta gerçek alt-saniye
    varış sırası YOKTUR - bu keyfi ama HER ZAMAN aynı yönde (çağıranın
    verdiği sırayla) bir tie-break'tir, uydurulmuş bir oran/ağırlık
    DEĞİLDİR.

    Algoritma (AŞAMA 22/41):
      - TÜM `arrivals` zaman sırasına göre kuyruğa (deque) ÖN-YÜKLENİR -
        her segment `[arrival_time, origin, count]`.
      - `server_count` boyutunda bir heap, her biri "bu sunucu ne zaman
        müsait olacak" zamanını tutar (başlangıçta ilk arrival anına
        eşitlenir - "sunucular baştan beri boş" varsayımı).
      - Kuyruk boşalana kadar: en erken müsait sunucuyu (heap'in tepesi)
        POP et, kuyruğun ÖNÜNDEKİ segment'ten TEK bir birim al, servis
        başlangıcını `max(sunucu_müsait_zamanı, birimin_arrival_time'ı)`
        olarak KENETLE (bir passenger, KENDİ arrival anından ÖNCE servise
        giremez - sunucu daha önce boşta bile olsa) - bu, kuyrukta bekleyen
        iş varken YENİ bir arrival'ı beklemeden mevcut işi işlemeye devam
        etmeyi (AŞAMA 10) VE gerçek FIFO/arrival-time sırasını AYNI ANDA
        garanti eder.
      - Bu, O(N log c) sunucu-pop/push işlemiyle (N = toplam kişi sayısı)
        TEK TEK passenger service mantığıyla MATEMATİKSEL OLARAK EŞDEĞER
        sonucu üretir - hiçbir Passenger ORM satırı/nesnesi YARATILMADAN.

    Dönen liste `completion_time`'a göre SIRALI değildir garanti edilmez
    hale gelmeyecek şekilde üretim sırasıyla eklenir (aynı sunucu dalgası
    içindeki tamamlanmalar zaten kronolojik sırayla oluşur); çağıran
    taraf gerekiyorsa `sorted(events, key=lambda e: e.completion_time)`
    kullanabilir.
    """
    if server_count <= 0:
        raise ValueError(f"server_count > 0 olmalı (alınan={server_count})")
    if service_time_minutes <= 0:
        raise ValueError(
            f"service_time_minutes > 0 olmalı (alınan={service_time_minutes})"
        )

    sorted_arrivals = sorted(arrivals, key=lambda item: item[0])
    if not sorted_arrivals:
        return []

    service_delta = timedelta(minutes=service_time_minutes)
    sentinel = sorted_arrivals[0][0]
    free_heap: list[datetime] = [sentinel] * server_count
    heapq.heapify(free_heap)

    # FIFO bekleme kuyruğu, TÜM arrival'lar zaman sırasıyla ÖN-YÜKLENİR
    # (her eleman [arrival_time, origin, remaining_count] - mutable liste,
    # bir segment'in bir kısmı servise alınınca kalan miktar YERİNDE
    # güncellenir, yeni segment açılmaz/parçalanmaz).
    queue: deque[list] = deque(
        [t, origin, count] for t, origin, count in sorted_arrivals if count > 0
    )

    events: list[ServiceEvent] = []

    while queue:
        free_time = heapq.heappop(free_heap)
        segment = queue[0]
        arrival_time, origin, remaining = segment

        # KENET: bir passenger KENDİ arrival anından ÖNCE servise
        # giremez, sunucu daha önceden boşta bile olsa (AŞAMA 10 - sunucu
        # bu bekleme sırasında ATIL DURMUYOR, sadece BU segment'e ait
        # değil; gerçek atıllık burada yeniden yaratılmaz, sadece bu
        # birimin KENDİ başlangıç anı doğru hesaplanır).
        service_start = max(free_time, arrival_time)
        completion = service_start + service_delta

        events.append(ServiceEvent(
            origin=origin,
            count=1.0,
            arrival_time=arrival_time,
            service_start_time=service_start,
            completion_time=completion,
        ))

        if remaining - 1 <= 0:
            queue.popleft()
        else:
            segment[2] = remaining - 1

        heapq.heappush(free_heap, completion)

    return _merge_adjacent_events(events)


def _merge_adjacent_events(events: list[ServiceEvent]) -> list[ServiceEvent]:
    """
    Aynı (origin, arrival_time, service_start_time, completion_time)
    dörtlüsüne sahip ardışık tekil-birim olayları TEK bir ServiceEvent'e
    (count toplanmış) birleştirir - çıktı boyutu O(N) değil O(benzersiz
    dalga sayısı) kalır; `simulate_fifo_queue`'nun kendi iç döngüsü
    teker teker (O(N log c)) çalışsa bile DIŞARI verilen sonuç şişmez.
    """
    if not events:
        return []

    merged: dict[tuple, float] = {}
    order: list[tuple] = []
    for event in events:
        key = (
            event.origin, event.arrival_time,
            event.service_start_time, event.completion_time,
        )
        if key not in merged:
            merged[key] = 0.0
            order.append(key)
        merged[key] += event.count

    return [
        ServiceEvent(
            origin=key[0], count=merged[key],
            arrival_time=key[1], service_start_time=key[2],
            completion_time=key[3],
        )
        for key in order
    ]


def simulate_passport(
    departure_arrivals: Sequence[tuple[datetime, float]],
    arrival_arrivals: Sequence[tuple[datetime, float]],
    departure_server_count: int,
    arrival_server_count: int,
    service_time_minutes: float,
    departure_dynamic: DynamicStaffingParams | None = None,
    arrival_dynamic: DynamicStaffingParams | None = None,
) -> dict[str, list[ServiceEvent] | list[tuple[datetime, int]] | None]:
    """
    ADIM (Airport-Scale Queue Capacity) - departure ve arrival passport
    ARTIK İKİ AYRI fiziksel havuz (AYRI `simulate_fifo_queue()` çağrısı,
    AYRI deque, AYRI server-availability heap'i). Eskiden (bkz. git
    history) TEK bir `tagged` listesi TEK bir heap'e giriyordu - bu
    ORTAK-havuz varsayımı airport-scale kuralı gereği KALDIRILDI: large
    bir havalimanında departure=20, arrival=30 server gibi FARKLI
    büyüklükte iki fiziksel kaynak olabiliyor, tek bir sayı bunu ifade
    edemez.

    departure_arrivals : uluslararası kalkış yolcusunun passport kuyruğuna
                          giriş zamanı + miktarı (mevcut `effective_time()`
                          ile AYNI an - bu fonksiyon YENİ bir zamanlama
                          varsaymaz, dışarıdan hazır liste alır).
    arrival_arrivals    : uluslararası varış yolcusunun passport'a giriş
                          zamanı (mevcut +15dk sabit buffer sonrası an).
    departure_server_count / arrival_server_count
                        : HER havuzun KENDİ, BAĞIMSIZ server sayısı -
                          `core/scoring.py:passport_departure_server_
                          count()`/`passport_arrival_server_count()`'tan
                          gelir (config'ten, hard-code YOK).

    Dönen sözlük ŞEKLİ DEĞİŞMEDİ (aşağı akışta gereksiz refactor'ı
    önlemek için - `engine.py` AYNI `{"departure": [...], "arrival":
    [...]}`'i tüketmeye devam eder):
      "departure" : passport'tan GERÇEKTEN çıkan kalkış-bağlantılı
                    yolcuların ServiceEvent listesi - `completion_time`
                    bu passenger'ların international security'ye
                    ARRIVAL zamanı olur (AŞAMA 9 - bkz.
                    `simulate_international_departure_journey`).
      "arrival"   : passport'tan çıkan varış-bağlantılı yolcular -
                    yolculuk burada BİTER, hiçbir kuyruğa aktarılmaz
                    (AŞAMA 30 - security'ye 0 kez girer). Departure
                    havuzunu HİÇ etkilemez, departure'ı da HİÇ etkilemez
                    (iki BAĞIMSIZ heap - arrival'ın kuyruğu ne kadar
                    dolu olursa olsun departure'ın kendi serverları
                    ETKİLENMEZ, ve tam tersi).
      "departure_schedule" / "arrival_schedule"
                  : ADIM (Dynamic LARGE Passport Staffing) - SADECE
                    `departure_dynamic`/`arrival_dynamic` verilmişse
                    o havuzun `simulate_fifo_queue_dynamic()`'ten dönen
                    (checkpoint_time, aktif_server_sayısı) programı;
                    verilmemişse (sabit, ESKİ davranış) `None`.

    departure_dynamic / arrival_dynamic
                        : ADIM (Dynamic LARGE Passport Staffing) -
                          `None` ise (varsayılan, geriye dönük uyumlu)
                          o havuz AYNEN eski `simulate_fifo_queue()`
                          (sabit `*_server_count`) yolunu kullanır -
                          davranış BİREBİR korunur. Verilirse (SADECE
                          LARGE + override'sız çağıranlar) o havuz
                          `simulate_fifo_queue_dynamic()`'e gider - İKİ
                          havuz TAMAMEN BAĞIMSIZ (Bölüm 3): departure
                          dynamic olabilirken arrival sabit kalabilir,
                          ve tam tersi.
    """
    departure_tagged = [(t, "departure", count) for t, count in departure_arrivals]
    arrival_tagged = [(t, "arrival", count) for t, count in arrival_arrivals]

    if departure_dynamic is not None:
        departure_events, departure_schedule = simulate_fifo_queue_dynamic(
            departure_tagged, departure_dynamic, service_time_minutes
        )
    else:
        departure_events = simulate_fifo_queue(
            departure_tagged, departure_server_count, service_time_minutes
        )
        departure_schedule = None

    if arrival_dynamic is not None:
        arrival_events, arrival_schedule = simulate_fifo_queue_dynamic(
            arrival_tagged, arrival_dynamic, service_time_minutes
        )
    else:
        arrival_events = simulate_fifo_queue(
            arrival_tagged, arrival_server_count, service_time_minutes
        )
        arrival_schedule = None

    return {
        "departure": departure_events,
        "arrival": arrival_events,
        "departure_schedule": departure_schedule,
        "arrival_schedule": arrival_schedule,
    }


def simulate_security(
    arrivals: Sequence[tuple[datetime, float]],
    lane_count: int,
    service_time_minutes: float,
    origin: str = "security",
) -> list[ServiceEvent]:
    """
    AŞAMA 6/7 - fiziksel olarak bağımsız TEK bir security kuyruğu
    (domestic VEYA international - hangisi olduğu ÇAĞIRAN tarafın hangi
    `lane_count`/`arrivals` verdiğine bağlıdır, bu fonksiyon ikisini
    ASLA karıştırmaz çünkü her çağrı kendi bağımsız `simulate_fifo_queue`
    işlemidir - iki AYRI heap, iki AYRI kuyruk).
    """
    tagged = [(t, origin, count) for t, count in arrivals]
    return simulate_fifo_queue(tagged, lane_count, service_time_minutes)


def simulate_international_departure_journey(
    departure_arrivals: Sequence[tuple[datetime, float]],
    arrival_arrivals: Sequence[tuple[datetime, float]],
    passport_departure_server_count: int,
    passport_arrival_server_count: int,
    passport_service_time_minutes: float,
    international_security_lane_count: int,
    security_service_time_minutes: float,
) -> dict[str, list[ServiceEvent]]:
    """
    AŞAMA 9 - passport -> international security TAM zincir, gerçek
    timestamp'lerle.

    international departure yolcusu passport'tan GERÇEKTEN çıktığı
    (`ServiceEvent.completion_time`) anda international security'ye
    ARRIVAL event'i olarak eklenir - passport security'yi BEKLEMEZ,
    security passport'u BEKLEMEZ (her ikisi de kendi `simulate_fifo_queue`
    çağrısı - security zaten kendi kuyruğunda bekleyen varsa AŞAMA 10
    gereği onu işlemeye devam eder, bu `simulate_fifo_queue`'nun kendi
    iç mantığıyla otomatik sağlanır).

    Dönen sözlük:
      "passport_departure" : passport'tan çıkan kalkış-bağlantılı olaylar
      "passport_arrival"   : passport'tan çıkan varış-bağlantılı olaylar
                              (security'ye HİÇ girmez)
      "security"           : international security'nin KENDİ
                              ServiceEvent'leri (arrival_time =
                              passport completion anı, service_start/
                              completion security'nin KENDİ sunucu
                              müsaitliğine göre)
    """
    passport = simulate_passport(
        departure_arrivals, arrival_arrivals,
        passport_departure_server_count, passport_arrival_server_count,
        passport_service_time_minutes,
    )

    security_arrivals = [
        (event.completion_time, event.count)
        for event in passport["departure"]
    ]
    security_events = simulate_security(
        security_arrivals,
        international_security_lane_count,
        security_service_time_minutes,
        origin="international",
    )

    return {
        "passport_departure": passport["departure"],
        "passport_arrival": passport["arrival"],
        "security": security_events,
    }


def total_count(events: Sequence[ServiceEvent]) -> float:
    """Conservation testlerinde kullanılan küçük yardımcı: toplam kişi."""
    return sum(e.count for e in events)


# ========================================================================
# ADIM (Dynamic LARGE Passport Staffing) - `simulate_fifo_queue()` (yukarıda,
# SABİT server sayılı) HİÇ DEĞİŞTİRİLMEDİ - bu, SADECE LARGE ölçekli
# passport havuzları (departure/arrival - AYRI AYRI, bkz. `simulate_
# passport()`'un yeni opsiyonel parametreleri) için AYRI bir path'tir.
# MEDIUM/SMALL/UNKNOWN ve explicit airport-override'lı LARGE alanları bu
# fonksiyonu HİÇ ÇAĞIRMAZ, eski `simulate_fifo_queue()` yoluna gider.
# ========================================================================

@dataclass(frozen=True)
class DynamicStaffingParams:
    """
    Deterministic (RANDOM YOK) dynamic staffing kontrol parametreleri.

    `default_server_count` : havuzun ASLA ALTINA DÜŞMEYECEĞİ taban -
                              ramp bu değerin altına inemez.
    `max_server_count`     : ramp bu değerin üstüne çıkamaz.
    `control_interval_minutes` / `look_ahead_minutes` / `target_
    utilization` / `ramp_step` : Bölüm 2'nin sabit kontrol parametreleri.
    """

    default_server_count: int
    max_server_count: int
    control_interval_minutes: int = 10
    look_ahead_minutes: int = 10
    target_utilization: float = 0.85
    ramp_step: int = 5


def simulate_fifo_queue_dynamic(
    arrivals: Sequence[tuple[datetime, str, float]],
    params: DynamicStaffingParams,
    service_time_minutes: float,
) -> tuple[list[ServiceEvent], list[tuple[datetime, int]]]:
    """
    `simulate_fifo_queue()` ile AYNI work-conserving FIFO çekirdek
    mantığı (arrival önceliği, sunucu-availability heap'i, `_merge_
    adjacent_events`) - TEK fark: sunucu sayısı sabit DEĞİL, deterministic
    kontrol noktalarında (`control_interval_minutes` aralıklarla, ilk
    arrival'dan başlayarak) backlog + look-ahead talebe göre [default,
    max] aralığında ±`ramp_step` adımlarla ayarlanır (Bölüm 4/5).

    SCALE-UP (Bölüm 4): yeni server slotları TAM OLARAK kontrol
    checkpoint zamanından itibaren müsait (`free_time = checkpoint_time`)
    - önceki hiçbir işi ETKİLEMEZ, backlog/passenger KAYBOLMAZ.

    SCALE-DOWN (Bölüm 5): GRACEFUL - `pending_retirements` sayacı
    "kaldırılması gereken ama HENÜZ boşta olmayan" server sayısını
    tutar; bir server ANCAK kendi o anki işini bitirip heap'e "boşta"
    olarak geri döndüğü anda (yani bu fonksiyonun onu YENİ bir işe
    ATAMAK üzere pop ettiği anda) retire edilir - aktif servis alan
    hiçbir passenger'ın servisi YARIDA KESİLMEZ. Bir sonraki checkpoint
    talebi tekrar artırırsa, henüz gerçekleşmemiş retirement'lar ÖNCE
    iptal edilir (yeni sunucu YARATILMADAN önce).

    Dönen `(events, schedule)`: `schedule`, her kontrol checkpoint'inde
    o andan itibaren geçerli olan (checkpoint_time, hedef_aktif_server_
    sayısı) çiftlerinin sıralı listesidir - `domain/dynamic_staffing.py:
    effective_capacity_by_hour()`'un zaman-ağırlıklı kapasite hesabı
    için girdisidir (Bölüm 9/10 - scoring'in KULLANDIĞI kapasite,
    simülasyonun GERÇEKTEN çalıştırdığı server sayısıyla TUTARLI olsun).
    """
    if params.default_server_count <= 0:
        raise ValueError(
            f"default_server_count > 0 olmalı (alınan={params.default_server_count})"
        )
    if params.max_server_count < params.default_server_count:
        raise ValueError(
            "max_server_count >= default_server_count olmalı "
            f"(alınan max={params.max_server_count}, default={params.default_server_count})"
        )
    if service_time_minutes <= 0:
        raise ValueError(
            f"service_time_minutes > 0 olmalı (alınan={service_time_minutes})"
        )

    sorted_arrivals = sorted(arrivals, key=lambda item: item[0])
    if not sorted_arrivals:
        return [], []

    service_delta = timedelta(minutes=service_time_minutes)
    control_delta = timedelta(minutes=params.control_interval_minutes)
    look_ahead_delta = timedelta(minutes=params.look_ahead_minutes)
    per_server_rate = 1.0 / service_time_minutes  # kişi/dk/server

    first_arrival = sorted_arrivals[0][0]
    active_count = params.default_server_count
    free_heap: list[datetime] = [first_arrival] * active_count
    heapq.heapify(free_heap)

    queue: deque[list] = deque(
        [t, origin, count] for t, origin, count in sorted_arrivals if count > 0
    )

    schedule: list[tuple[datetime, int]] = [(first_arrival, active_count)]
    next_checkpoint = first_arrival + control_delta
    pending_retirements = 0

    def _apply_checkpoint(checkpoint_time: datetime) -> None:
        nonlocal active_count, pending_retirements
        backlog = sum(seg[2] for seg in queue if seg[0] <= checkpoint_time)
        lookahead_end = checkpoint_time + look_ahead_delta
        lookahead_demand = sum(
            seg[2] for seg in queue
            if checkpoint_time < seg[0] <= lookahead_end
        )
        total_relevant = backlog + lookahead_demand
        if total_relevant <= 0 or per_server_rate <= 0 or params.target_utilization <= 0:
            needed_servers = 0
        else:
            required_rate = total_relevant / params.look_ahead_minutes
            needed_servers = math.ceil(
                required_rate / (per_server_rate * params.target_utilization)
            )

        ramp = max(-params.ramp_step, min(params.ramp_step, needed_servers - active_count))
        target = max(
            params.default_server_count,
            min(params.max_server_count, active_count + ramp),
        )
        delta = target - active_count
        if delta > 0:
            cancel = min(delta, pending_retirements)
            pending_retirements -= cancel
            to_add = delta - cancel
            for _ in range(to_add):
                heapq.heappush(free_heap, checkpoint_time)
        elif delta < 0:
            pending_retirements += -delta
        active_count = target

    events: list[ServiceEvent] = []

    while queue:
        segment = queue[0]
        heap_min = free_heap[0]
        imminent_time = max(heap_min, segment[0])
        while next_checkpoint <= imminent_time:
            _apply_checkpoint(next_checkpoint)
            schedule.append((next_checkpoint, active_count))
            next_checkpoint += control_delta
            heap_min = free_heap[0]
            imminent_time = max(heap_min, segment[0])

        free_time = heapq.heappop(free_heap)

        if pending_retirements > 0:
            # GRACEFUL SCALE-DOWN: bu server TAM OLARAK şimdi (kendi
            # önceki işini bitirip) boşta kaldı - yeni bir işe ATANMADAN
            # retire edilir (Bölüm 5 - hiçbir aktif servis kesilmedi,
            # bu server zaten boştaydı).
            pending_retirements -= 1
            continue

        service_start = max(free_time, segment[0])
        completion = service_start + service_delta

        events.append(ServiceEvent(
            origin=segment[1],
            count=1.0,
            arrival_time=segment[0],
            service_start_time=service_start,
            completion_time=completion,
        ))

        if segment[2] - 1 <= 0:
            queue.popleft()
        else:
            segment[2] -= 1

        heapq.heappush(free_heap, completion)

    return _merge_adjacent_events(events), schedule
