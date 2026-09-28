
from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence


@dataclass(frozen=True)
class ServiceEvent:

    origin: str
    count: float
    arrival_time: datetime
    service_start_time: datetime
    completion_time: datetime
    # flight_key -> bu event'e ait GERÇEK yolcu sayısı (toplamı her zaman
    # `count`'a eşittir). Tek flight'lı bir cohort için {key: count};
    # birden fazla flight'ın AYNI (arrival, service_start, completion)
    # üzerinde birleştiği durumda her flight'ın PAYI korunur - "kaç
    # flight katkı yaptı" değil "her biri kaçar yolcu" bilgisini taşır,
    # bu yüzden downstream (ör. passport completion -> security_intl)
    # aktarımlarında ÇİFTE SAYIM olmaz. Salt metadata - wait/service_
    # start/completion hesabını hiç ETKİLEMEZ.
    source_flight_keys: dict[str, float] | None = None

    @property
    def wait_minutes(self) -> float:
        return (self.service_start_time - self.arrival_time).total_seconds() / 60.0


def _as_key_shares(key, segment_total_count: float) -> dict[str, float] | None:
    """Bir segment key'ini TEK KİŞİLİK (1 unit) orantısal paya çevirir.
    `key` bir string ise (ham show-up/release cohort, TEK flight) o
    flight'a %100 pay verilir. `key` bir dict ise (üst aşamadan - ör.
    passport completion'dan - miras alınan per-flight dağılım) segment'in
    ORİJİNAL toplamına göre orantılı pay hesaplanır. `_merge_adjacent_
    events` bu payları TOPLADIĞINDA orijinal per-flight toplamlar
    KORUNUR (bkz. modül testleri)."""
    if not key:
        return None
    if isinstance(key, str):
        return {key: 1.0}
    if isinstance(key, dict):
        if segment_total_count <= 0:
            return None
        return {k: v / segment_total_count for k, v in key.items()}
    keys = list(key)
    if not keys:
        return None
    share = 1.0 / len(keys)
    return {k: share for k in keys}


def _normalize_tagged_arrivals(
    arrivals: Sequence[tuple],
) -> list[tuple[datetime, str, float, object]]:
    normalized = []
    for item in arrivals:
        t, origin, count = item[0], item[1], item[2]
        key = item[3] if len(item) > 3 else None
        normalized.append((t, origin, count, key))
    return normalized


def simulate_fifo_queue(
    arrivals: Sequence[tuple[datetime, str, float]] | Sequence[tuple[datetime, str, float, object]],
    server_count: int,
    service_time_minutes: float,
) -> list[ServiceEvent]:
    if server_count <= 0:
        raise ValueError(f"server_count > 0 olmalı (alınan={server_count})")
    if service_time_minutes <= 0:
        raise ValueError(
            f"service_time_minutes > 0 olmalı (alınan={service_time_minutes})"
        )

    normalized = _normalize_tagged_arrivals(arrivals)
    sorted_arrivals = sorted(normalized, key=lambda item: item[0])
    if not sorted_arrivals:
        return []

    service_delta = timedelta(minutes=service_time_minutes)
    sentinel = sorted_arrivals[0][0]
    free_heap: list[datetime] = [sentinel] * server_count
    heapq.heapify(free_heap)

    queue: deque[list] = deque(
        [t, origin, count, key, count] for t, origin, count, key in sorted_arrivals if count > 0
    )

    events: list[ServiceEvent] = []

    while queue:
        free_time = heapq.heappop(free_heap)
        segment = queue[0]
        arrival_time, origin, remaining, key, original_total = segment

        service_start = max(free_time, arrival_time)
        completion = service_start + service_delta

        events.append(ServiceEvent(
            origin=origin,
            count=1.0,
            arrival_time=arrival_time,
            service_start_time=service_start,
            completion_time=completion,
            source_flight_keys=_as_key_shares(key, original_total),
        ))

        if remaining - 1 <= 0:
            queue.popleft()
        else:
            segment[2] = remaining - 1

        heapq.heappush(free_heap, completion)

    return _merge_adjacent_events(events)


def _merge_adjacent_events(events: list[ServiceEvent]) -> list[ServiceEvent]:
    if not events:
        return []

    merged: dict[tuple, float] = {}
    merged_keys: dict[tuple, dict[str, float] | None] = {}
    order: list[tuple] = []
    for event in events:
        key = (
            event.origin, event.arrival_time,
            event.service_start_time, event.completion_time,
        )
        if key not in merged:
            merged[key] = 0.0
            merged_keys[key] = None
            order.append(key)
        merged[key] += event.count
        if event.source_flight_keys:
            existing = merged_keys[key] or {}
            for flight_key, share in event.source_flight_keys.items():
                existing[flight_key] = existing.get(flight_key, 0.0) + share
            merged_keys[key] = existing

    return [
        ServiceEvent(
            origin=key[0], count=merged[key],
            arrival_time=key[1], service_start_time=key[2],
            completion_time=key[3], source_flight_keys=merged_keys[key],
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
    departure_tagged = [
        (t, "departure", *rest) for t, *rest in departure_arrivals
    ]
    arrival_tagged = [
        (t, "arrival", *rest) for t, *rest in arrival_arrivals
    ]

    if departure_dynamic is not None:
        departure_events, departure_schedule, departure_checkpoint_log = simulate_fifo_queue_dynamic(
            departure_tagged, departure_dynamic, service_time_minutes
        )
    else:
        departure_events = simulate_fifo_queue(
            departure_tagged, departure_server_count, service_time_minutes
        )
        departure_schedule = None
        departure_checkpoint_log = None

    if arrival_dynamic is not None:
        arrival_events, arrival_schedule, arrival_checkpoint_log = simulate_fifo_queue_dynamic(
            arrival_tagged, arrival_dynamic, service_time_minutes
        )
    else:
        arrival_events = simulate_fifo_queue(
            arrival_tagged, arrival_server_count, service_time_minutes
        )
        arrival_schedule = None
        arrival_checkpoint_log = None

    return {
        "departure": departure_events,
        "arrival": arrival_events,
        "departure_schedule": departure_schedule,
        "arrival_schedule": arrival_schedule,
        "departure_checkpoint_log": departure_checkpoint_log,
        "arrival_checkpoint_log": arrival_checkpoint_log,
    }


def simulate_security(
    arrivals: Sequence[tuple[datetime, float]],
    lane_count: int,
    service_time_minutes: float,
    origin: str = "security",
    dynamic: DynamicStaffingParams | None = None,
):
    # `dynamic` VERİLMEZSE (mevcut TÜM çağrı yerleri - security_dom,
    # security_combined, ve dynamic olmayan security_intl) davranış ve
    # dönüş TİPİ (düz `list[ServiceEvent]`) TAMAMEN ESKİSİ GİBİ - hiçbir
    # mevcut test/çağrı kırılmaz. `dynamic` VERİLİRSE (SADECE MEGA
    # security_intl) `simulate_passport`'taki ile AYNI desen: 3'lü
    # tuple - (events, schedule, checkpoint_log). Lane/service-time
    # matematiği (`simulate_fifo_queue`/`simulate_fifo_queue_dynamic`)
    # DEĞİŞMEDİ - burada sadece HANGİ fonksiyonun çağrılacağına karar
    # veriliyor.
    tagged = [(t, origin, *rest) for t, *rest in arrivals]
    if dynamic is not None:
        return simulate_fifo_queue_dynamic(tagged, dynamic, service_time_minutes)
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
    passport = simulate_passport(
        departure_arrivals, arrival_arrivals,
        passport_departure_server_count, passport_arrival_server_count,
        passport_service_time_minutes,
    )

    security_arrivals = [
        (event.completion_time, event.count, event.source_flight_keys)
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
    return sum(e.count for e in events)


def virtual_arrival_wait(
    events: Sequence[ServiceEvent],
    probe_time: datetime,
    server_count: int,
    service_time_minutes: float,
) -> float:
    server_count = int(round(server_count))
    if server_count <= 0 or service_time_minutes <= 0:
        return 0.0

    busy_free_times: list[datetime] = []
    backlog = 0.0
    for event in events:
        if event.service_start_time <= probe_time < event.completion_time:
            busy_free_times.extend([event.completion_time] * int(round(event.count)))
        elif event.arrival_time <= probe_time < event.service_start_time:
            backlog += event.count

    busy_free_times.sort()
    if len(busy_free_times) > server_count:
        busy_free_times = busy_free_times[:server_count]
    idle_count = server_count - len(busy_free_times)

    heap = busy_free_times + [probe_time] * idle_count
    heapq.heapify(heap)

    service_delta = timedelta(minutes=service_time_minutes)
    for _ in range(int(round(backlog))):
        free_time = heapq.heappop(heap)
        service_start = max(free_time, probe_time)
        heapq.heappush(heap, service_start + service_delta)

    virtual_free_time = heap[0] if heap else probe_time
    virtual_service_start = max(virtual_free_time, probe_time)
    return max(0.0, (virtual_service_start - probe_time).total_seconds() / 60.0)



@dataclass(frozen=True)
class DynamicStaffingParams:

    default_server_count: int
    max_server_count: int
    control_interval_minutes: int = 10
    look_ahead_minutes: int = 10
    target_utilization: float = 0.85
    ramp_step: int = 5
    scale_down_backlog_floor_minutes: float = 30.0
    # ADIM (Proaktif Arrival Lookahead) - `None` ise (passport_dep,
    # security_intl - varsayılan, ESKİ davranışla AYNI) hiçbir etkisi
    # YOK. Sadece passport_arr için verilir (ör. 20 dakika): normal
    # `look_ahead_minutes` (5dk) penceresine EK olarak, DAHA UZUN bir
    # pencerede de "gereken gişe" hesaplanır ve İKİSİNİN BÜYÜĞÜ
    # kullanılır - böylece henüz backlog oluşmadan, ileride gelecek
    # büyük bir varış dalgası (uçak inişinden 10-35dk sonra passport'a
    # yürüme) önceden görülüp gişeler ERKEN açılabilir.
    extended_look_ahead_minutes: int | None = None
    # ADIM (Discrete Operational Levels) - `None` ise (varsayılan, ESKİ
    # davranış - generic/test kullanımı) `ramp_step`'e dayalı SÜREKLİ
    # (herhangi bir tamsayı) ramp KORUNUR. Bir tuple verilirse (MEGA'nın
    # 3 gerçek süreci) aktif sayı SADECE bu listedeki değerlerden biri
    # olabilir ve HER checkpoint'te en fazla BİR seviye (index) hareket
    # edilir - `ramp_step` bu modda YOK SAYILIR (seviye listesindeki
    # komşu index farkı otomatik olarak "bir adım" tanımını verir).
    allowed_levels: tuple[int, ...] | None = None


@dataclass(frozen=True)
class DynamicStaffingCheckpoint:
    # Audit/raporlama için ZENGİN checkpoint kaydı - production
    # matematiğini (schedule/FIFO) HİÇ ETKİLEMEZ, sadece `_apply_
    # checkpoint()`'in İÇİNDE zaten hesaplanan ama önceden dışarı
    # sızdırılmayan ara değerleri (backlog, lookahead, needed_servers,
    # reason) audit'e yazılabilecek şekilde expose eder.
    checkpoint_time: datetime
    previous_count: int
    new_count: int
    backlog: float
    lookahead_demand: float
    needed_servers: int
    # ADIM (Discrete Operational Levels) - `needed_servers` HAM matematik
    # sonucu (ör. 31); `target_operational_level` bunun bir sonraki
    # uygun sabit seviyeye yuvarlanmış hâli (ör. 35) - "gereken sayı" ile
    # "uygulanan seviye" AYRI. `allowed_levels` verilmemişse (eski/generic
    # mod) bu alan `needed_servers` ile AYNI kalır (yuvarlama yok).
    target_operational_level: int
    ramp: int
    pending_retirements_after: int
    reason: str


def simulate_fifo_queue_dynamic(
    arrivals: Sequence[tuple[datetime, str, float]],
    params: DynamicStaffingParams,
    service_time_minutes: float,
) -> tuple[list[ServiceEvent], list[tuple[datetime, int]], list[DynamicStaffingCheckpoint]]:
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

    normalized = _normalize_tagged_arrivals(arrivals)
    sorted_arrivals = sorted(normalized, key=lambda item: item[0])
    if not sorted_arrivals:
        return [], [], []

    service_delta = timedelta(minutes=service_time_minutes)
    control_delta = timedelta(minutes=params.control_interval_minutes)
    look_ahead_delta = timedelta(minutes=params.look_ahead_minutes)
    extended_look_ahead_delta = (
        timedelta(minutes=params.extended_look_ahead_minutes)
        if params.extended_look_ahead_minutes else None
    )
    per_server_rate = 1.0 / service_time_minutes

    first_arrival = sorted_arrivals[0][0]
    active_count = params.default_server_count
    free_heap: list[datetime] = [first_arrival] * active_count
    heapq.heapify(free_heap)

    queue: deque[list] = deque(
        [t, origin, count, key, count] for t, origin, count, key in sorted_arrivals if count > 0
    )

    schedule: list[tuple[datetime, int]] = [(first_arrival, active_count)]
    checkpoint_log: list[DynamicStaffingCheckpoint] = []
    next_checkpoint = first_arrival + control_delta
    pending_retirements = 0

    def _needed_servers_for(total_relevant: float, window_minutes: float) -> int:
        if total_relevant <= 0 or per_server_rate <= 0 or params.target_utilization <= 0:
            return 0
        required_rate = total_relevant / window_minutes
        return math.ceil(required_rate / (per_server_rate * params.target_utilization))

    def _apply_checkpoint(checkpoint_time: datetime) -> None:
        nonlocal active_count, pending_retirements
        backlog = sum(seg[2] for seg in queue if seg[0] <= checkpoint_time)
        lookahead_end = checkpoint_time + look_ahead_delta
        lookahead_demand = sum(
            seg[2] for seg in queue
            if checkpoint_time < seg[0] <= lookahead_end
        )
        total_relevant = backlog + lookahead_demand
        needed_servers = _needed_servers_for(total_relevant, params.look_ahead_minutes)

        # ADIM (Proaktif Arrival Lookahead) - Bölüm 11: sadece `extended_
        # look_ahead_minutes` set edilmişse (passport_arr) ek, DAHA UZUN
        # pencereli bir ihtiyaç hesabı da yapılır ve İKİSİNİN BÜYÜĞÜ
        # kullanılır - backlog henüz oluşmadan ileride gelecek büyük bir
        # varış dalgası önceden görülebilsin diye.
        extended_lookahead_demand = 0.0
        extended_needed_servers = 0
        if extended_look_ahead_delta is not None:
            extended_end = checkpoint_time + extended_look_ahead_delta
            extended_lookahead_demand = sum(
                seg[2] for seg in queue
                if checkpoint_time < seg[0] <= extended_end
            )
            extended_needed_servers = _needed_servers_for(
                backlog + extended_lookahead_demand, params.extended_look_ahead_minutes
            )
        effective_needed_servers = max(needed_servers, extended_needed_servers)

        previous_count = active_count
        max_capacity_per_minute = params.max_server_count * per_server_rate
        forced_no_scale_down = False

        if params.allowed_levels is not None:
            # ADIM (Discrete Operational Levels) - DB max'a göre FİLTRELENMİŞ
            # sabit seviye listesi; runtime aktif sayı SADECE bu listeden
            # biri olabilir, HER checkpoint'te en fazla BİR seviye hareket
            # eder (ramp_step burada kullanılmaz - "bir adım" = listede bir
            # komşu index).
            effective_levels = sorted(
                lvl for lvl in params.allowed_levels if lvl <= params.max_server_count
            )
            if not effective_levels:
                effective_levels = [params.default_server_count]
            candidates = [lvl for lvl in effective_levels if lvl >= effective_needed_servers]
            # "gereken sayı" HAM matematikten bir sonraki uygun seviyeye
            # yuvarlanmış hâli - audit'te AYRI bir alan olarak kalıyor,
            # aşağıdaki tek-adım kısıtlamasından ETKİLENMEZ.
            target_operational_level = min(candidates) if candidates else effective_levels[-1]

            if active_count in effective_levels:
                current_idx = effective_levels.index(active_count)
            else:
                current_idx = min(
                    range(len(effective_levels)),
                    key=lambda i: abs(effective_levels[i] - active_count),
                )

            if target_operational_level > active_count:
                new_idx = min(current_idx + 1, len(effective_levels) - 1)
            elif target_operational_level < active_count:
                new_idx = max(current_idx - 1, 0)
            else:
                new_idx = current_idx
            stepped_target = effective_levels[new_idx]
            ramp = stepped_target - active_count

            if max_capacity_per_minute > 0:
                backlog_clearance_minutes_at_max = backlog / max_capacity_per_minute
                if backlog_clearance_minutes_at_max >= params.scale_down_backlog_floor_minutes and ramp < 0:
                    forced_no_scale_down = True
                    stepped_target = active_count
                    ramp = 0
            target = stepped_target
        else:
            # Eski, sürekli (herhangi bir tamsayı) ramp mantığı - generic/
            # test kullanımı için AYNEN korunuyor.
            target_operational_level = effective_needed_servers
            ramp = max(-params.ramp_step, min(params.ramp_step, effective_needed_servers - active_count))
            if max_capacity_per_minute > 0:
                backlog_clearance_minutes_at_max = backlog / max_capacity_per_minute
                if backlog_clearance_minutes_at_max >= params.scale_down_backlog_floor_minutes:
                    if ramp < 0:
                        forced_no_scale_down = True
                    ramp = max(ramp, 0)
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

        # ADIM (Section 17 - Audit Reason) - her checkpoint için okunabilir
        # bir sınıflandırma: production kararını (ramp/target) HİÇ
        # ETKİLEMEZ, sadece SONUCU açıklayan bir etiket.
        if forced_no_scale_down:
            reason = "backlog_pressure"
        elif ramp < 0:
            reason = "scale_down"
        elif ramp == 0:
            reason = "normal_load"
        elif effective_needed_servers >= params.max_server_count:
            reason = "peak_pressure"
        elif extended_needed_servers > needed_servers:
            reason = "lookahead_pressure"
        elif backlog > lookahead_demand:
            reason = "backlog_pressure"
        else:
            reason = "demand_threshold"

        checkpoint_log.append(DynamicStaffingCheckpoint(
            checkpoint_time=checkpoint_time,
            previous_count=previous_count,
            new_count=active_count,
            backlog=backlog,
            lookahead_demand=lookahead_demand + extended_lookahead_demand,
            needed_servers=effective_needed_servers,
            target_operational_level=target_operational_level,
            ramp=target - previous_count,
            pending_retirements_after=pending_retirements,
            reason=reason,
        ))

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
            source_flight_keys=_as_key_shares(segment[3], segment[4]),
        ))

        if segment[2] - 1 <= 0:
            queue.popleft()
        else:
            segment[2] -= 1

        heapq.heappush(free_heap, completion)

    return _merge_adjacent_events(events), schedule, checkpoint_log
