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

BU ADIMDA ENTEGRASYON YOK: bu modül mevcut `engine.py`/`predict_airport()`/
API/frontend'e BAĞLANMADI - mevcut saatlik graph/API contract'ı bu turda
DEĞİŞMİYOR (görev kapsamı). Bu, bağımsız test edilebilir, gelecekteki bir
entegrasyon turu için hazır bir ÇEKİRDEKTİR.
"""

from __future__ import annotations

import heapq
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
    effective_server_count: int,
    service_time_minutes: float,
) -> dict[str, list[ServiceEvent]]:
    """
    AŞAMA 8/9/17 - ORTAK fiziksel passport havuzu (4 gişe x 2 görevli
    = 8 efektif server, TEK `simulate_fifo_queue` çağrısı - sunucular
    departure/arrival için AYRI AYRI yaratılmaz, aynı heap'i paylaşır).

    departure_arrivals : uluslararası kalkış yolcusunun passport kuyruğuna
                          giriş zamanı + miktarı (mevcut `effective_time()`
                          ile AYNI an - bu fonksiyon YENİ bir zamanlama
                          varsaymaz, dışarıdan hazır liste alır).
    arrival_arrivals    : uluslararası varış yolcusunun passport'a giriş
                          zamanı (mevcut +15dk sabit buffer sonrası an).

    Dönen sözlük:
      "departure" : passport'tan GERÇEKTEN çıkan kalkış-bağlantılı
                    yolcuların ServiceEvent listesi - `completion_time`
                    bu passenger'ların international security'ye
                    ARRIVAL zamanı olur (AŞAMA 9 - bkz.
                    `simulate_international_departure_journey`).
      "arrival"   : passport'tan çıkan varış-bağlantılı yolcular -
                    yolculuk burada BİTER, hiçbir kuyruğa aktarılmaz
                    (AŞAMA 30 - security'ye 0 kez girer).
    """
    tagged = [
        (t, "departure", count) for t, count in departure_arrivals
    ] + [
        (t, "arrival", count) for t, count in arrival_arrivals
    ]
    events = simulate_fifo_queue(tagged, effective_server_count, service_time_minutes)
    return {
        "departure": [e for e in events if e.origin == "departure"],
        "arrival": [e for e in events if e.origin == "arrival"],
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
    passport_server_count: int,
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
        passport_server_count, passport_service_time_minutes,
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
