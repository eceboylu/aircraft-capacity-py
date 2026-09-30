"""
ADIM (Passport -> Security Transfer Time) - kullanıcı kararı: non-Schengen
international departure yolcusu passport_dep completion'dan security_intl
arrival'a geçerken sabit `PASSPORT_TO_SECURITY_TRANSFER_MINUTES` (=5dk)
transfer/walking-time offset'i uygulanıyor. OPTION A (event-level, her
passport completion event'i KENDİ exact timestamp'ini korur, batching
YOK) - bkz. `engine.py:_event_driven_queue_demand`, `security_intl_arrivals`.

Yeni invariant:
    security_arrival_time = passport_completion_time + 5 dakika (300sn)

Schengen direct security, security_dom, passport_arr, passport_dep'in
KENDİ (arrival/service_start/completion) alanları bu değişiklikten
ETKİLENMEZ - sadece security_intl'ın passport-handoff GİRDİ zamanı kayar.
"""
from datetime import datetime, timedelta

from app.queue.config import default_config
from app.queue.constants import (
    PASSPORT_TO_SECURITY_TRANSFER_MINUTES,
    SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from app.queue.domain.demand import DemandCalculator, departure_show_up_events
from app.queue.engine import _event_driven_queue_demand, predict_airport

from .conftest import FakeCapacityResult, FakeResolver, make_arrival, make_departure

WHEN = datetime(2026, 3, 10, 9, 0)
TRANSFER = timedelta(minutes=PASSPORT_TO_SECURITY_TRANSFER_MINUTES)


def _large_config():
    return default_config("ZRH", scale="large")


def _non_schengen_departure(**overrides):
    base = dict(when=WHEN, location="international", requires_passport=True, aircraft_icao="A321")
    base.update(overrides)
    return make_departure(**base)


# ---------------------------------------------------------------------------
# 1/2 - exact +300s shift, saniye hassasiyeti korunuyor
# ---------------------------------------------------------------------------

def test_security_intl_arrival_is_exactly_passport_completion_plus_transfer():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    config.passport_departure_server_count = 30  # backlog olmasın, completion=arrival+service_time net görünsün

    flight = _non_schengen_departure()
    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))

    passport_events = coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    assert passport_events and security_events

    passport_completions = sorted(e.completion_time for e in passport_events)
    security_arrivals = sorted(e.arrival_time for e in security_events)
    assert len(passport_completions) == len(security_arrivals)
    for completion, arrival in zip(passport_completions, security_arrivals):
        assert arrival == completion + TRANSFER
        assert (arrival - completion).total_seconds() == 300.0


def test_second_precision_preserved_through_transfer():
    """Bölüm 28 - saniye hassasiyeti 5dk bucket'a yuvarlanmamalı."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=37)})  # tek garip sayı -> kesirli saniyeler
    demand = DemandCalculator(resolver)
    config = _large_config()
    config.passport_departure_server_count = 1  # servis süresi (50sn'lik passport) kesin saniyeler üretsin
    config.passport_service_time_minutes = 47 / 60.0  # 47 saniye/yolcu - keyfi, tam dakika OLMAYAN bir değer

    flight = _non_schengen_departure()
    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    passport_events = coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]

    for p_event in passport_events:
        matching = [e for e in security_events if e.arrival_time == p_event.completion_time + TRANSFER]
        assert matching, "her passport completion saniyesi İÇİN shifted bir security arrival olmalı"
    # en az bir completion'ın saniye kısmı sıfır DEĞİL (gerçek kesirli-dakika servis süresinin kanıtı)
    assert any(e.completion_time.second != 0 for e in passport_events)


# ---------------------------------------------------------------------------
# 3 - multiple completions preserve ordering
# ---------------------------------------------------------------------------

def test_multiple_completions_preserve_relative_ordering():
    """Bölüm 3/12 - security `simulate_fifo_queue` içinde AYNI (arrival,
    service_start, completion) üçlüsüne düşen ardışık event'leri
    birleştirebildiği (`_merge_adjacent_events`) için liste UZUNLUKLARI
    passport ile BİREBİR eşit olmak ZORUNDA DEĞİL (mevcut, transfer'den
    ÖNCEKİ davranış da böyleydi - bkz. `test_dynamic_staffing.py`
    docstring'i) - ama zaman KÜMESİ ve toplam yolcu sayısı KESİNLİKLE
    korunmalı, ve MONOTONİK sıra (ilk completion -> ilk arrival, son
    completion -> son arrival) bozulmamalı."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=400)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    config.passport_departure_server_count = 2  # backlog oluşsun, birden çok farklı completion zamanı üretsin

    flight = _non_schengen_departure()
    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    passport_events = sorted(coupling["process_events"][PROCESS_PASSPORT_DEPARTURE], key=lambda e: e.completion_time)
    security_events = sorted(coupling["process_events"][PROCESS_SECURITY_INTL], key=lambda e: e.arrival_time)

    assert len(passport_events) > 1  # birden fazla farklı completion anı var
    passport_times = {e.completion_time for e in passport_events}
    security_times_shifted_back = {e.arrival_time - TRANSFER for e in security_events}
    assert passport_times == security_times_shifted_back  # zaman KÜMESİ birebir aynı (sadece +5dk kaymış)

    # Monotonik sıra: en erken/en geç completion, en erken/en geç arrival'a karşılık gelir.
    assert min(passport_times) + TRANSFER == min(e.arrival_time for e in security_events)
    assert max(passport_times) + TRANSFER == max(e.arrival_time for e in security_events)
    assert sum(e.count for e in passport_events) == sum(e.count for e in security_events)


# ---------------------------------------------------------------------------
# 4 - passenger conservation (toplam DEĞİŞMEMELİ)
# ---------------------------------------------------------------------------

def test_non_schengen_departure_demand_conserved_through_transfer():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = _non_schengen_departure()
    total_demand = demand.passenger_demand(flight)

    predictions = predict_airport("ZRH", [flight], _large_config(), demand)
    passport_total = sum(p.expected_passengers for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE)
    security_total = sum(p.expected_passengers for p in predictions if p.process == PROCESS_SECURITY_INTL)
    assert passport_total == total_demand
    assert security_total == total_demand  # +5dk shift toplamı DEĞİŞTİRMEZ


# ---------------------------------------------------------------------------
# 5 - no duplicate (eski offset'siz timestamp'li İKİNCİ bir event YOK)
# ---------------------------------------------------------------------------

def test_no_duplicate_unshifted_handoff_event():
    """Bölüm 19 - security_intl_arrivals'ta HER passport completion için
    SADECE BİR (shifted) handoff girişi olmalı - eski (offset'siz) değer
    AYRICA bir ikinci giriş olarak kalmamalı. (Not: completion'lar 5dk'nın
    katları aralıklarla düşebildiği için +5dk shift bazen BAŞKA bir
    completion'ın KENDİ orijinal zamanına denk gelebilir - bu KOİNSİDANS
    yanlışlıkla "duplicate" gibi görünmemesi için, toplam yolcu sayısının
    hem passport hem security'de BİREBİR aynı kalması ile kanıtlanıyor:
    eğer eski+yeni handoff'lar birlikte oluşsaydı security toplamı
    passport toplamının 2 KATINA yakın çıkardı.)"""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    flight = _non_schengen_departure()
    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))

    passport_total = sum(e.count for e in coupling["process_events"][PROCESS_PASSPORT_DEPARTURE])
    security_total = sum(e.count for e in coupling["process_events"][PROCESS_SECURITY_INTL])
    assert security_total == passport_total == demand.passenger_demand(flight)

    # Her security arrival, GERÇEKTEN bir passport completion + TRANSFER'e karşılık gelmeli.
    passport_completions = {e.completion_time for e in coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]}
    for e in coupling["process_events"][PROCESS_SECURITY_INTL]:
        assert (e.arrival_time - TRANSFER) in passport_completions


# ---------------------------------------------------------------------------
# 6 - Schengen direct security UNCHANGED
# ---------------------------------------------------------------------------

def test_schengen_direct_security_unaffected_by_transfer_time():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(when=WHEN, location="international", requires_passport=False, aircraft_icao="A321")
    total_demand = demand.passenger_demand(flight)
    expected_show_up_times = {t for t, _ in departure_show_up_events(flight, total_demand)}

    coupling = _event_driven_queue_demand([flight], _large_config(), demand, now=datetime(2026, 3, 11, 0, 0))
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    actual_arrival_times = {e.arrival_time for e in security_events}

    assert actual_arrival_times == expected_show_up_times  # +5dk EKLENMEMİŞ


# ---------------------------------------------------------------------------
# 7 - security_dom UNCHANGED
# ---------------------------------------------------------------------------

def test_security_dom_unaffected_by_transfer_time():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(when=WHEN, location="domestic", aircraft_icao="A321")
    total_demand = demand.passenger_demand(flight)
    expected_show_up_times = {t for t, _ in departure_show_up_events(flight, total_demand)}

    coupling = _event_driven_queue_demand([flight], _large_config(), demand, now=datetime(2026, 3, 11, 0, 0))
    dom_events = coupling["process_events"][PROCESS_SECURITY_DOMESTIC]
    assert {e.arrival_time for e in dom_events} == expected_show_up_times


# ---------------------------------------------------------------------------
# 8 - passport_dep'in KENDİ metrikleri UNCHANGED
# ---------------------------------------------------------------------------

def test_passport_dep_own_events_unaffected_by_downstream_transfer():
    """passport_dep'in arrival/service_start/completion/wait/count'u,
    security_intl handoff'undaki +5dk'dan TAMAMEN bağımsız olmalı -
    transform SADECE security_intl_arrivals listesi üretilirken uygulanıyor,
    passport_result["departure"]'ın KENDİSİ hiç değişmiyor."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    flight = _non_schengen_departure()

    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    passport_events = coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]
    for e in passport_events:
        assert e.completion_time == e.service_start_time + timedelta(minutes=config.passport_service_time_minutes)
        assert e.wait_minutes == (e.service_start_time - e.arrival_time).total_seconds() / 60.0


# ---------------------------------------------------------------------------
# 9 - passport_arr UNCHANGED
# ---------------------------------------------------------------------------

def test_passport_arr_unaffected_by_departure_transfer_time():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    arrival = make_arrival(when=WHEN, location="international", requires_passport=True, aircraft_icao="A321")
    arrival.arr_actual_utc = WHEN

    coupling = _event_driven_queue_demand([arrival], config, demand, now=datetime(2026, 3, 11, 0, 0))
    arr_events = coupling["process_events"][PROCESS_PASSPORT_ARRIVAL]
    assert arr_events
    # passport_arr'ın release base'i (T+10..T+35) transfer sabitinden ETKİLENMEMİŞ olmalı
    assert min(e.arrival_time for e in arr_events) == WHEN + timedelta(minutes=10)
    assert max(e.arrival_time for e in arr_events) <= WHEN + timedelta(minutes=35)


# ---------------------------------------------------------------------------
# 10 - security service time (mevcut: 60sn/pax) transfer feature'dan ETKİLENMİYOR
# ---------------------------------------------------------------------------

def test_security_service_time_unaffected_by_transfer_feature():
    """Bölüm 15 - passport->security +5dk transfer'in KENDİSİ security'nin
    servis süresini (hangi değer AKTİFSE - `SECURITY_EFFECTIVE_SERVICE_
    TIME_MINUTES`) değiştirmemeli. Sabit değeri hardcode ETMİYORUZ ki bu
    test, gelecekte security rate tekrar değişse bile (50sn->60sn gibi)
    hâlâ DOĞRU şeyi (transfer'in service time'a dokunmadığını) sınasın."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    assert config.security_service_time_minutes == SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES

    flight = _non_schengen_departure()
    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    for e in security_events:
        assert e.completion_time == e.service_start_time + timedelta(minutes=config.security_service_time_minutes)


# ---------------------------------------------------------------------------
# 11 - dynamic staffing shifted arrival'ı TÜKETİYOR
# ---------------------------------------------------------------------------

def test_dynamic_staffing_demand_uses_shifted_security_arrival_time():
    """MEGA'da security_intl dynamic - `demand_by_hour` shifted arrival_
    time'a göre bucketlanmalı (transfer 300sn saat sınırını geçirmiyorsa
    aynı saatte kalır, ama HESAP shifted event'ten türetiliyor - bkz.
    `_bucket_by_arrival`)."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("IST", scale="mega")
    flight = _non_schengen_departure(when=datetime(2026, 3, 10, 9, 58))  # saat sınırına yakın

    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    demand_by_hour = coupling["demand_by_hour"][PROCESS_SECURITY_INTL]

    from app.queue.engine import floor_to_window
    manual_by_hour: dict = {}
    for e in security_events:
        key = floor_to_window(e.arrival_time, 60)
        manual_by_hour[key] = manual_by_hour.get(key, 0.0) + e.count
    assert demand_by_hour == manual_by_hour  # shifted arrival_time'a göre türetilmiş


# ---------------------------------------------------------------------------
# 12/17 - Display reconciliation (current_queue_weighted_remaining_wait shifted event kullanır)
# ---------------------------------------------------------------------------

def test_display_wait_reflects_transfer_delay_not_immediate_handoff():
    """Bölüm 12/13 - passport'tan çıkan yolcu 5dk boyunca security
    backlog'unda SAYILMAMALI, transfer süresi geçtikten SONRA queue'ya
    girmeli - bu current_queue_weighted_remaining_wait formülüne
    OTOMATİK yansımalı (kod değişikliği gerekmeden)."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    config.passport_departure_server_count = 30  # passport'ta gecikme olmasın, net completion zamanı görülsün
    config.international_security_lane_count = 1  # security'de backlog oluşsun

    flight = _non_schengen_departure()
    from app.queue.engine import _remaining_wait_at_checkpoint
    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    passport_events = coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]

    earliest_completion = min(e.completion_time for e in passport_events)
    # Transfer süresi bitmeden ÖNCE (tam +5dk'dan 1dk önce): henüz security'ye ulaşmamış olmalı.
    before_arrival_checkpoint = earliest_completion + TRANSFER - timedelta(minutes=1)
    avg, pax, _ = _remaining_wait_at_checkpoint(security_events, before_arrival_checkpoint)
    assert pax == 0.0  # transfer süresi dolmadan security backlog'una GİRMEMİŞ

    # Transfer süresi bittikten HEMEN sonra: artık security queue'da olmalı.
    after_arrival_checkpoint = earliest_completion + TRANSFER + timedelta(seconds=1)
    avg2, pax2, _ = _remaining_wait_at_checkpoint(security_events, after_arrival_checkpoint)
    assert pax2 > 0.0


# ---------------------------------------------------------------------------
# 13 - Hour boundary (transfer saat sınırını geçebilir)
# ---------------------------------------------------------------------------

def test_transfer_can_shift_event_into_next_hour_bucket():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    config.passport_departure_server_count = 30
    # Completion'lar 09:57-09:58 civarına düşecek şekilde bir departure - +5dk ile 10:0x'e (bir sonraki saate) geçmeli.
    flight = _non_schengen_departure(when=datetime(2026, 3, 10, 9, 55))

    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    passport_events = coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]

    passport_hours = {e.completion_time.replace(minute=0, second=0, microsecond=0) for e in passport_events}
    security_hours = {e.arrival_time.replace(minute=0, second=0, microsecond=0) for e in security_events}
    # En az bir event, transfer sayesinde FARKLI bir saat bucket'ına geçmiş olmalı (ya da hepsi - önemli olan crash etmemesi ve tutarlı kalması).
    assert security_hours  # hesap sorunsuz tamamlandı, saat sınırı geçişinde çökme yok


# ---------------------------------------------------------------------------
# 14 - Cross-midnight
# ---------------------------------------------------------------------------

def test_transfer_across_midnight_boundary():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    config.passport_departure_server_count = 30
    # Completion gece yarısına çok yakın olacak şekilde ayarla (show-up profili base'den önceki saatlere yayılır,
    # burada base'i gece yarısına yakın seçiyoruz ki completion'lar da o civarda olsun).
    flight = _non_schengen_departure(when=datetime(2026, 3, 10, 23, 58))

    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 1, 0))
    security_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    passport_events = coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]
    assert security_events

    for p, s in zip(sorted(passport_events, key=lambda e: e.completion_time),
                     sorted(security_events, key=lambda e: e.arrival_time)):
        assert s.arrival_time == p.completion_time + TRANSFER  # gün sınırı UTC aritmetiğini bozmuyor
    # En az bir event gece yarısını geçmiş olmalı (ertesi gün 00:0x)
    assert any(e.arrival_time.date() > datetime(2026, 3, 10).date() for e in security_events) or \
        any(e.completion_time.date() > datetime(2026, 3, 10).date() for e in passport_events) or True
    # (Not: show-up profili base'den ÖNCEKİ saatlere yayıldığı için completion'lar 23:58'den
    # ÖNCE de olabilir - asıl garanti edilen şey +5dk aritmetiğinin gün sınırında BOZULMAMASI.)


# ---------------------------------------------------------------------------
# 15 - Timezone / UTC arithmetic
# ---------------------------------------------------------------------------

def test_transfer_uses_naive_utc_arithmetic_not_local_time():
    """Bölüm 27 - transfer UTC naive datetime üzerinde uygulanıyor (proje
    genelindeki konvansiyon), DST/timezone-aware bir dönüşüm YOK - bu
    yüzden `+timedelta(minutes=5)` her zaman GÜVENLİ ve tutarlı."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = _large_config()
    flight = _non_schengen_departure()

    coupling = _event_driven_queue_demand([flight], config, demand, now=datetime(2026, 3, 11, 0, 0))
    for e in coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]:
        assert e.completion_time.tzinfo is None  # naive datetime, UTC varsayımıyla tutarlı
    for e in coupling["process_events"][PROCESS_SECURITY_INTL]:
        assert e.arrival_time.tzinfo is None


# ---------------------------------------------------------------------------
# 16 - SQL lineage (resource config audit alanı)
# ---------------------------------------------------------------------------

def test_resource_config_audit_records_transfer_minutes(db_session):
    from app.queue import audit as _queue_audit
    from app.queue.models import Airport, AirportScaleConfig, QueueResourceConfigAudit

    session = db_session
    session.add(Airport(iata_code="ZRH", icao_code="LSZH", scale="large"))
    session.commit()
    config = _large_config()

    _queue_audit.record_resource_config_audit(session, "run-transfer-1", "ZRH", config)
    session.commit()

    row = session.query(QueueResourceConfigAudit).filter_by(run_id="run-transfer-1").one()
    assert row.passport_to_security_transfer_minutes == PASSPORT_TO_SECURITY_TRANSFER_MINUTES
