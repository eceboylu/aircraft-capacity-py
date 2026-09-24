"""
ADIM 5H - Minimal read-only sorgu katmanı (frontend/HTTP için).

Bu modül GÖRSELLEŞTİRME İÇİN veri hazırlar - hiçbir yoğunluk/kapasite
HESABI yapmaz. Erlang-C, passenger demand, effective_time, risk,
confidence, reasons - bunların TEK doğruluk kaynağı `engine.py` +
`core/scoring.py` + `reasons/detector.py`'dir; burada SADECE zaten
hesaplanmış `QueuePrediction` satırları okunup JSON-uyumlu sözlüklere
çevrilir.

`reporting.py` (AŞAMA 9) ile KARIŞTIRILMAMALI: o modül birden fazla
`QueuePrediction` penceresini post-hoc (worst-risk/max-wait) BİRLEŞTİRİR.
Burada ise ham `QueuePrediction` satırları (opsiyonel bir `since`
filtresiyle) HİÇ birleştirilmeden, production motorunun ürettiği
GERÇEK pencere genişliğiyle (bkz. `DEMAND_WINDOW_MINUTES`, ADIM 6D-2
Hourly Migration'dan itibaren 60 dk/saatlik) doğrudan döner.
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .constants import (
    DEMAND_WINDOW_MINUTES,
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_ORDER,
    RISK_UNKNOWN,
)
from .domain.operational_day import (
    operational_day_window,
    resolve_airport_timezone,
)
from .models import Airport, Flight, QueuePrediction

# --- Risk -> UI durum etiketi (TEK yer, SADECE sunum) ------------------
#
# Backend'in ürettiği risk değeri (LOW/MEDIUM/HIGH/CRITICAL/UNKNOWN)
# BURADA değişmez - sadece kullanıcıya gösterilecek İNGİLİZCE etikete
# eşlenir (ADIM: Passenger-Facing Risk Threshold v2 - kullanıcı talebi:
# tüm UI metinleri İngilizce, 5 backend değeri artık 5 AYRI etikete
# eşlenir, önceki sürümdeki HIGH/CRITICAL "ÇOK YOĞUN" sıkıştırması
# KALDIRILDI - artık her risk seviyesi kendi ayrı etiketini taşır):
#   LOW -> "Normal", MEDIUM -> "Getting Busy", HIGH -> "Busy",
#   CRITICAL -> "Very Busy", UNKNOWN -> "Unknown" (baseline eksikliğini
#   normal yoğunlukla KESİNLİKLE karıştırmaz - ADIM 5H'nin en kritik
#   kuralı, bkz. modül testleri).
RISK_TO_UI_LABEL = {
    RISK_LOW: "Normal",
    RISK_MEDIUM: "Getting Busy",
    RISK_HIGH: "Busy",
    RISK_CRITICAL: "Very Busy",
    RISK_UNKNOWN: "Unknown",
}


def ui_label_for_risk(risk: str | None) -> str | None:
    """
    Ham backend risk değerini UI etiketine çevirir.

    `risk is None` (bu havalimanı/süreç için HİÇ pencere yok) `None`
    döner - çağıran taraf (frontend) bunu "No data" ile göstermeli,
    "Normal" ile DEĞİL. Bilinmeyen bir risk string'i gelirse (ileride
    yeni bir değer eklenirse) olduğu gibi geçirilir - sessizce
    Normal'e düşürülmez.
    """
    if risk is None:
        return None
    return RISK_TO_UI_LABEL.get(risk, risk)


def overall_status(*risks: str | None) -> dict:
    """
    ADIM 6A §13-16 (ADIM G'de N-YOLLU hale getirildi) - GENEL havalimanı
    yoğunluğu. SADECE presentation/aggregation - hiçbir YENİ risk hesabı
    yapmaz, security/passport formüllerine dokunmaz.

    Kural: verilen süreçlerin CURRENT riskinden EN YÜKSEK severity
    kazanır (`RISK_ORDER` - AŞAMA 9'da "saatin en kötü penceresi" için
    kullanılan AYNI sıralama, burada TEKRAR TANIMLANMADI):

        LOW < MEDIUM < HIGH < CRITICAL

    UNKNOWN bir "gerçek risk" DEĞİLDİR - `RISK_ORDER`'da en altta
    durur, bu yüzden diğer taraflar gerçek bir risk taşıyorsa asla onu
    ezmez (`HIGH + UNKNOWN -> HIGH`). Sadece TÜM taraflar UNKNOWN
    (veya biri UNKNOWN biri no-data) olduğunda sonuç UNKNOWN'dır
    (`UNKNOWN + UNKNOWN -> UNKNOWN`, label "BİLİNMİYOR").

    Girdilerin HEPSİ `None` ise (bu havalimanı için hiçbir sürecin
    HİÇBİRİNDE satır yok) `{"risk": None, "label": None}` döner -
    ADIM 5H'nin "no-data ASLA NORMAL değildir, hatta BİLİNMİYOR ile de
    karıştırılmaz" ayrımı overall'da da korunur; frontend bunu "Veri
    bulunamadı" ile gösterir.

    ADIM G: artık İKİ (security+passport, GERİYE DÖNÜK UYUMLU çağrı
    şekli) veya ÜÇ (domestic_security+international_security+passport,
    yeni GENEL HAVALİMANI YOĞUNLUĞU tanımı) risk ile çağrılabilir -
    fonksiyonun kendisi kaç argüman verildiğinden bağımsız, sadece
    "verilenlerin en kötüsü" mantığını uygular.

    Sadece `current` (ŞU ANKİ pencere) risk değerleri kullanılır -
    serideki geçmiş/gelecek pencerelerin maksimumu KULLANILMAZ (bir
    havalimanı geçmişte bir yerde HIGH göstermiş diye sürekli HIGH
    görünmez).
    """
    candidates = [r for r in risks if r is not None]
    if not candidates:
        return {"risk": None, "label": None}

    worst = max(candidates, key=lambda r: RISK_ORDER.get(r, -1))
    return {"risk": worst, "label": ui_label_for_risk(worst)}


def tracked_airports(session) -> list[str]:
    """
    İzlenen havalimanları - hiçbir hardcoded liste YOK. Alfabetik sıralı.

    ADIM (Zero-Flight Airport Visibility) - ÖNCEDEN SADECE `QueuePrediction`
    satırı olan havalimanları listeleniyordu. Bu, GERÇEKTEN ingest edilmiş
    (en az bir `Flight` satırı üretmiş) ama o günün operasyonel-gün
    filtresine göre BUGÜN hiç uçuşu olmayan bir havalimanının (`run_
    predictions()`'ın "flights boşsa atla" davranışı, bkz. o fonksiyon)
    dizin'den TAMAMEN KAYBOLMASINA yol açıyordu - `airport_predictions()`/
    `process_series()` bu havalimanı için ZATEN doğru, tam 24-saatlik
    sıfır-talep günü üretebiliyor olsa bile (`_pad_series_to_24_hours` -
    `day_start`/`tz` SADECE `Airport` tablosundan gelir, Flight/
    QueuePrediction geçmişine bağlı DEĞİLDİR), dizin listede hiç
    GÖRÜNMÜYORDU (canlı `/api/airports/EYP/predictions` ile doğrulandı -
    sıfır flight/prediction geçmişiyle bile 5/5 grafik 24 dolu/LOW/
    Normal pencere döndürüyor, SADECE eski `tracked_airports()` onu
    dizine hiç EKLEMİYORDU).

    DÜZELTME: artık `Flight.airport_iata` (en az bir kez GERÇEKTEN
    ingest edilmiş havalimanı - `engine.airport_codes()`'un ZATEN
    kullandığı AYNI "desteklenen evren" tanımı) İLE `QueuePrediction.
    airport_iata`'nın BİRLEŞİMİ kullanılır - global `Airport` referans
    tablosundaki (binlerce havalimanı) HİÇBİRİ otomatik olarak
    İFŞA EDİLMEZ, sadece gerçekten en az bir kez refresh edilmiş
    havalimanlar (mevcut/eski davranışın ÜST KÜMESİ, hiçbir mevcut
    satır listeden ÇIKARILMAZ).

    GERİYE DÖNÜK UYUMLULUK: bu fonksiyonun/`/api/airports`'un dönüş
    şekli (düz string listesi) DEĞİŞTİRİLMEDİ - havalimanı adı
    isteyen tüketiciler `airport_directory()`/`/api/airports/directory`
    kullanmalı (ADIM 6A-UI-2).
    """
    flight_codes = session.execute(
        select(Flight.airport_iata).distinct()
    ).scalars().all()
    prediction_codes = session.execute(
        select(QueuePrediction.airport_iata).distinct()
    ).scalars().all()
    codes = {code for code in (*flight_codes, *prediction_codes) if code}
    return sorted(codes)


# ADIM 6C §B - İKİNCİL isim kaynağı: `Airport.airport_name` (BİRİNCİL
# kaynak, flight_airports.sql'den) bir kod için NULL/eksikse burası
# denenir. BİLEREK BOŞ - şu an (61/61 izlenen havalimanı) birincil
# kaynak zaten her kodu karşılıyor (bkz. ADIM 6C testleri); burada
# TEK TEK hardcode edilmiş bir IATA->ad sözlüğü İCAT ETMEK yerine,
# gerçek/doğrulanmış ek bir isim kaynağı bulununca BURAYA eklenecek
# TEK merkezi yapı hazır tutuluyor. Frontend'de dağınık if/else YOK -
# üçüncü (ve son) katman zaten sadece IATA kodudur.
_LOCAL_AIRPORT_NAME_FALLBACK: dict[str, str] = {}


def airport_directory(session) -> list[dict]:
    """
    ADIM 6A-UI-2 §3 / ADIM 6C §B - İzlenen havalimanları AD'larıyla
    birlikte, ÜÇ katmanlı öncelik:

      1) `Airport.airport_name` (flight_airports.sql'den GERÇEK veri)
      2) `_LOCAL_AIRPORT_NAME_FALLBACK` (tek merkezi, doğrulanmış ek
         kaynak - şu an boş, bkz. yukarıdaki not)
      3) `None` - çağıran taraf (frontend) bu durumda SADECE IATA
         kodunu göstermeli; sahte bir ad ÜRETİLMEZ.

    Bir havalimanının BURADA YER ALMASI hiçbir zaman isme bağlı
    DEĞİLDİR - `tracked_airports()`'un döndürdüğü HER kod, adı
    bulunamasa bile listede kalır (bkz. testler).
    """
    codes = tracked_airports(session)
    if not codes:
        return []

    primary_names = dict(session.execute(
        select(Airport.iata_code, Airport.airport_name)
        .where(Airport.iata_code.in_(codes))
    ).all())

    def resolve_name(code: str) -> str | None:
        return primary_names.get(code) or _LOCAL_AIRPORT_NAME_FALLBACK.get(code)

    return [{"iata": code, "name": resolve_name(code)} for code in codes]


def _floor_to_window(moment: datetime, window_minutes: int = DEMAND_WINDOW_MINUTES) -> datetime:
    """
    `engine.py:floor_to_window()` ile FONKSİYONEL OLARAK AYNI (bir
    zamanı içinde bulunduğu 15 dk penceresinin başlangıcına yuvarlar)
    ama BİLİNÇLİ OLARAK BURADA TEKRAR TANIMLANDI, `engine.py`'den
    import EDİLMEDİ: bu modülün "hiçbir risk/kapasite/Erlang-C hesabı
    barındıran modülü import etmez" mimari sınırını (bkz. modül
    başlığı, `test_h_api_module_does_not_import_calculation_engine`)
    tek bir trivial tarih-yuvarlama fonksiyonu için BOZMAMAK adına.
    Sabit (`DEMAND_WINDOW_MINUTES`) yine de TEK yerden (constants.py)
    geliyor - "15" değeri iki yerde AYRI AYRI tanımlı DEĞİL.
    """
    minute = (moment.minute // window_minutes) * window_minutes
    return moment.replace(minute=minute, second=0, microsecond=0)


def traffic_breakdown(
    session, airport_iata: str, now: datetime | None = None
) -> dict | None:
    """
    ADIM 6A-UI-2 §6-8 - Genel operasyon dağılımı (departure/arrival,
    domestic/international). SADECE `Flight` tablosundan OKUR - hiçbir
    Erlang-C/security risk/passport risk/baseline/passenger demand/
    capacity hesabına DOKUNMAZ, YENİDEN HESAPLAMAZ.

    Zaman referansı BİLİNÇLİ bir seçimdir: security/passport'un
    kullandığı `effective_time()` (buffer'lı) BURADA KULLANILMAZ -
    o fonksiyon SADECE ilgili sürece giren uçuşlar için tanımlıdır
    (bkz. domain/demand.py) ve domestic arrival gibi HİÇBİR sürece
    girmeyen uçuşlar için hiç anlamlı değildir - "international =
    passport", "departure = security" gibi bir KISAYOL/varsayım
    kurmadan TÜM uçuşlara EŞİT uygulanabilecek tek zaman kendi HAM
    scheduled zamanıdır (departure -> dep_scheduled_utc, arrival ->
    arr_scheduled_utc), production'ın paylaştığı 15 dk ızgarasına
    göre gruplanır.

    "current" pencere, `_pick_current()` ile AYNI üç kollu mantıkla
    (kapsayan / en yakın geçmiş / en yakın gelecek) seçilir - sayfanın
    diğer "current" göstergeleriyle (security/passport) TUTARLI
    kalması için. Bu havalimanı için hiçbir zaman damgalı uçuş yoksa
    (`None` girdi) breakdown üretilemez - sahte/sıfır değer ÜRETMEK
    yerine `None` döner; çağıran taraf bunu açıkça göstermeli.
    """
    now = now if now is not None else _utcnow()

    rows = session.execute(
        select(Flight).where(
            Flight.airport_iata == airport_iata,
            Flight.status.notin_(EXCLUDED_STATUSES),
        )
    ).scalars().all()

    dated = []
    for flight in rows:
        moment = (
            flight.dep_scheduled_utc if flight.direction == DIRECTION_DEPARTURE
            else flight.arr_scheduled_utc
        )
        if moment is not None:
            dated.append((flight, moment))

    if not dated:
        return None

    window_starts = sorted({_floor_to_window(m) for _, m in dated})
    containing = [
        w for w in window_starts
        if w <= now < w + timedelta(minutes=DEMAND_WINDOW_MINUTES)
    ]
    if containing:
        chosen = containing[-1]
    else:
        past = [w for w in window_starts if w <= now]
        chosen = past[-1] if past else window_starts[0]

    window_end = chosen + timedelta(minutes=DEMAND_WINDOW_MINUTES)
    in_window = [f for f, m in dated if chosen <= m < window_end]

    return {
        "window_start": chosen.isoformat(),
        "window_end": window_end.isoformat(),
        "departures": sum(1 for f in in_window if f.direction == DIRECTION_DEPARTURE),
        "arrivals": sum(1 for f in in_window if f.direction == DIRECTION_ARRIVAL),
        "domestic": sum(1 for f in in_window if f.location == LOCATION_DOMESTIC),
        "international": sum(1 for f in in_window if f.location == LOCATION_INTERNATIONAL),
    }


def _to_local_iso(moment_utc: datetime, tz) -> str | None:
    """
    ADIM (24-Hour Graph Timezone Display) - naive UTC bir `datetime`'ı
    (`QueuePrediction.window_start`/`window_end` ile AYNI birim -
    engine.py'nin tamamı naive UTC, bkz. `domain_now()` docstring'i)
    havalimanının GERÇEK yerel saatine çevirir. `tz` (bir `zoneinfo.
    ZoneInfo`) çözülemiyorsa (Bölüm 2 - "raw behavior'a güvenli
    fallback") `None` döner - UYDURMA bir offset ÜRETİLMEZ, çağıran
    taraf `window_start_local: None` bırakır ve frontend kendi
    fallback'iyle (`window.window_start_local || window.window_start`)
    ham UTC'yi gösterir.

    `zoneinfo` kullanır (sabit +2/+3 HARD-CODE edilmedi) - DST'li
    (Europe/Paris, Europe/Zurich gibi) havalimanlarında yılın doğru
    gününe göre GERÇEK offset otomatik uygulanır.
    """
    if tz is None:
        return None
    aware_utc = moment_utc.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(tz).isoformat()


def _window_to_dict(row: QueuePrediction, tz=None) -> dict:
    """
    Tek bir `QueuePrediction` satırının JSON-uyumlu görünümü.
    Hiçbir alan burada YENİDEN HESAPLANMAZ.

    `window_start`/`window_end` (CANONICAL, internal/UTC - DEĞİŞMEDİ,
    geriye dönük uyumlu) YANINDA, `tz` verilirse (havalimanının GERÇEK
    yerel saat dilimi çözülebiliyorsa) `window_start_local`/`window_end_
    local` da eklenir - frontend'in grafik label'ı için OKUMASI GEREKEN
    alan (bkz. `_synthetic_zero_demand_window` - padded sıfır-talep
    bucket'ları da AYNI iki alanı taşır, frontend real/padded farkını
    BİLMEZ).
    """
    return {
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "window_start_local": _to_local_iso(row.window_start, tz),
        "window_end_local": _to_local_iso(row.window_end, tz),
        "flight_count": row.flight_count,
        "expected_passengers": row.expected_passengers,
        "baseline_ratio": row.baseline_ratio,
        "flight_ratio": row.flight_ratio,
        "passenger_ratio": row.passenger_ratio,
        "utilization": row.utilization,
        "estimated_wait_minutes": row.estimated_wait_minutes,
        "risk": row.risk,
        "risk_label": ui_label_for_risk(row.risk),
        "confidence": row.confidence,
        "reasons": json.loads(row.reasons or "[]"),
        "calculated_at": row.calculated_at.isoformat(),
    }


def _utcnow() -> datetime:
    """Naive UTC 'şimdi' - `QueuePrediction.window_start` da naive UTC'dir (bkz. constants.py)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _synthetic_zero_demand_window(window_start: datetime, window_end: datetime, tz=None) -> dict:
    """
    ADIM (24-Hour Graph) - Bölüm B: bir saatte GERÇEKTEN hiçbir uçuş/
    event olmadığı KESİN olduğunda (bkz. `_pad_series_to_24_hours`
    çağıran taraf garantisi - sadece bu havalimanı için tahmin
    üretimi GERÇEKTEN çalıştıysa, yani `windows` boş DEĞİLSE VEYA
    zaten bu süreç için hiç satır YOKSA bile timezone çözülebiliyorsa)
    üretilen, SIFIR-TALEP bucket'ı.

    Bu UYDURMA bir tahmin DEĞİLDİR - `flight_count=0`/`expected_
    passengers=0` zaten doğru (o saat hiç uçuş yoksa gerçek toplam
    zaten sıfırdır); `estimated_wait_minutes=0.0`/`risk=LOW` bunun
    doğrudan, tartışmasız sonucudur (talep yok -> kuyruk yok). Backend
    `core/scoring.py`'nin KENDİSİ hâlâ bu sonucu ÜRETİR (demand=0 ->
    rho=0 -> LOW/0.0) - burada SADECE o hesabın demand=0 için HER
    ZAMAN aynı olacağı sonucu, performans amacıyla (her boş saat için
    gerçek motoru tekrar çağırmadan) tekrarlanır. `confidence=1.0`:
    "bu saatte talep yoktur" gözlemi varsayıma dayanmaz, doğrudan
    `Flight` tablosunun kendisinden (o saatte hiç kayıt yok) gelir.

    ADIM (24-Hour Graph Timezone Display) - Bölüm 7: padded (sıfır-talep)
    bucket'lar da GERÇEK satırlarla AYNI `window_start_local`/`window_end_
    local` alanlarını taşır - frontend real/padded ayrımını hiç BİLMEZ,
    ikisi de AYNI contract'ı kullanır.
    """
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_start_local": _to_local_iso(window_start, tz),
        "window_end_local": _to_local_iso(window_end, tz),
        "flight_count": 0,
        "expected_passengers": 0,
        "baseline_ratio": None,
        "flight_ratio": None,
        "passenger_ratio": None,
        "utilization": 0.0,
        "estimated_wait_minutes": 0.0,
        "risk": RISK_LOW,
        "risk_label": ui_label_for_risk(RISK_LOW),
        "confidence": 1.0,
        "reasons": [],
        "calculated_at": None,
    }


def _pick_highest_severity_entry(entries: list[dict]) -> dict:
    """
    ADIM (Shared Worst-Of Reducer) - Bölüm 6D-2 H2'nin "en kötü kazanır"
    kuralının TEK, paylaşılan uygulaması: birden fazla pencere-sözlüğü
    arasından önce en yüksek severity'yi (`RISK_ORDER`), sonra (aynı
    severity'yi taşıyan birden fazla giriş varsa) gerçek/finite
    `estimated_wait_minutes`'ı EN YÜKSEK olanı seçer; hiçbirinde finite
    wait yoksa (hepsi wait-modelsiz, ör. security) severity'yi taşıyan
    İLK girişi döner.

    `_merge_overall_series` (SÜREÇLER ARASI birleştirme - ör. aynı saatte
    domestic_security/international_security/passport) İLE
    `_merge_duplicate_local_hour` (AYNI sürecin DST fall-back'te
    tekrarlanan yerel saatindeki İKİ GERÇEK/sentetik pencereyi
    birleştirme, Bölüm 4) TEK bu reducer'ı kullanır - iki YERDE ayrı,
    tutarsız bir risk/wait kuralı İCAT EDİLMEZ.
    """
    top_severity = max(RISK_ORDER.get(e["risk"], -1) for e in entries)
    top_entries = [e for e in entries if RISK_ORDER.get(e["risk"], -1) == top_severity]
    with_wait = [e for e in top_entries if e["estimated_wait_minutes"] is not None]
    return max(with_wait, key=lambda e: e["estimated_wait_minutes"]) if with_wait else top_entries[0]


def _merge_duplicate_local_hour(first: dict, second: dict) -> dict:
    """
    ADIM (DST Fall-Back Exact24) - Bölüm 4: sonbahar geri-alma gününde
    AYNI yerel duvar-saati (ör. Europe/Amsterdam'da "02:00-03:00") İKİ
    farklı GERÇEK UTC saatinde gerçekleşir. `first` bunların KRONOLOJİK
    olarak ÖNCE geleni, `second` SONRA geleni - ikisi de zaten
    `_window_to_dict()`/`_synthetic_zero_demand_window()` çıktısı bir
    sözlüktür (gerçek `QueuePrediction` satırı VEYA o saatte hiç uçuş
    yoksa sıfır-talep sentetik pencere). HİÇBİRİ SİLİNMEZ/ATLANMAZ -
    "hiçbir gerçek QueuePrediction silently drop edilmemeli" (Bölüm 4).

    `flight_count`/`expected_passengers` TOPLANIR (additive) - bu iki
    GERÇEK saat GERÇEKTEN farklı yolcuları/uçuşları temsil eder, bu
    yüzden passenger conservation İÇİN toplamaları GEREKİR (Bölüm 5 -
    "PASSENGER CONSERVATION"). Risk/wait/utilization/confidence/reasons/
    ratio alanları YENİ bir formülle YENİDEN hesaplanmaz - mevcut
    `_pick_highest_severity_entry()` (Bölüm 6D-2 H2 ile PAYLAŞILAN AYNI
    "en kötü kazanır" kuralı) ile ikisinden "en kötü"sü seçilip AYNEN
    taşınır; uydurma bir ortalama/yeni oran ÜRETİLMEZ - "mevcut backend
    semantics'i kullan... yeni rastgele/worst-of kural uydurma" (Bölüm 4).

    `window_start`/`window_end` (canonical UTC) bu görünür bucket'ın
    GERÇEKTE kapladığı TAM 2 saatlik UTC aralığını taşır (`min(start)`..
    `max(end)`) - "24 GÖRÜNÜR bucket" sözü SADECE frontend'in TEK bir
    yerel-saat etiketi görmesi anlamına gelir, GERÇEK 2 saatlik veri
    aralığı `window_start`/`window_end`'den hâlâ hesaplanabilir kalır
    (Bölüm 8 - API shape/semantics net kalmalı). `window_start_local`/
    `window_end_local` ise tekrarlanan yerel saati TEK etiket olarak
    taşır (`first`in başlangıcı, `second`in bitişi - "02:00"/"03:00").
    `calculated_at` en GÜNCEL (`max`) olanı taşır.
    """
    winner = _pick_highest_severity_entry([first, second])
    calculated_candidates = [
        v for v in (first["calculated_at"], second["calculated_at"]) if v is not None
    ]
    return {
        "window_start": min(first["window_start"], second["window_start"]),
        "window_end": max(first["window_end"], second["window_end"]),
        "window_start_local": first.get("window_start_local"),
        "window_end_local": second.get("window_end_local"),
        "flight_count": first["flight_count"] + second["flight_count"],
        "expected_passengers": first["expected_passengers"] + second["expected_passengers"],
        "baseline_ratio": winner["baseline_ratio"],
        "flight_ratio": winner["flight_ratio"],
        "passenger_ratio": winner["passenger_ratio"],
        "utilization": winner["utilization"],
        "estimated_wait_minutes": winner["estimated_wait_minutes"],
        "risk": winner["risk"],
        "risk_label": winner["risk_label"],
        "confidence": winner["confidence"],
        "reasons": winner["reasons"],
        "calculated_at": max(calculated_candidates) if calculated_candidates else None,
    }


def _synthetic_gap_window(transition_utc: datetime, local_start_naive: datetime, local_end_naive: datetime) -> dict:
    """
    ADIM (DST Spring-Forward Exact24) - Bölüm 4: ilkbahar ileri-alma
    gününde bir yerel duvar-saati (ör. "02:00-03:00") HİÇBİR GERÇEK UTC
    anına karşılık gelmez (saat yerel 01:59'dan doğrudan 03:00'e
    ATLAR). Bu slot için UYDURMA bir queue/demand verisi ÜRETİLMEZ
    (Bölüm 4 - "synthetic bucket queue data uydurmamalı") -
    `_synthetic_zero_demand_window()` ile AYNI sıfır-talep/LOW/
    confidence=1.0 sözleşmesini taşır (o yerel saatte GERÇEKLEŞMİŞ hiçbir
    zaman aralığı yoktur, dolayısıyla tanım gereği sıfır talep).

    `window_start`/`window_end` (canonical UTC) YİNE gerçek bir UTC anı
    taşır - `transition_utc`, bu yerel saatin "sıfır genişlikte" var
    olduğu TEK UTC an (önceki/sonraki gerçek saatin sınırı) - Bölüm 8'in
    "window_start HER ZAMAN UTC" sözleşmesi bu sentetik bucket için de
    BOZULMAZ. `window_start_local`/`window_end_local` ise bu UTC anının
    KENDİSİNDEN `astimezone` ile türetilmez (bu, var OLMAYAN saati değil,
    bitişteki GERÇEK saati gösterirdi) - doğrudan, NAIVE aritmetikle
    hesaplanan "duvar saati ne gösterirdi" etiketini taşır (deterministic,
    UYDURMA queue verisi İÇERMEZ, SADECE bir metin etiketi).
    """
    key_iso = transition_utc.isoformat()
    return {
        "window_start": key_iso,
        "window_end": key_iso,
        "window_start_local": local_start_naive.isoformat(),
        "window_end_local": local_end_naive.isoformat(),
        "flight_count": 0,
        "expected_passengers": 0,
        "baseline_ratio": None,
        "flight_ratio": None,
        "passenger_ratio": None,
        "utilization": 0.0,
        "estimated_wait_minutes": 0.0,
        "risk": RISK_LOW,
        "risk_label": ui_label_for_risk(RISK_LOW),
        "confidence": 1.0,
        "reasons": [],
        "calculated_at": None,
    }


def _build_exact24_display_windows(
    windows: list[dict],
    grid_start: datetime,
    grid_end: datetime,
    day_start: datetime,
    tz,
) -> list[dict]:
    """
    ADIM (Calculation Horizon != Display Horizon - Bölüm 3/4): `windows`
    (bu havalimanı/süreç/gün için ZATEN filtrelenmiş - `_belongs_to_
    current_operational_day` + `_within_display_bounds` - GERÇEK pencere
    sözlükleri, `[grid_start, grid_end)` UTC aralığında) GERÇEK yerel
    günün UZUNLUĞUNA (`grid_end - grid_start` - DST geçiş günlerinde 23/
    25, aksi halde 24 saat) göre TAM 24 GÖRÜNÜR bucket'a projekte edilir:

      - NORMAL gün (24 saat): `_pad_series_to_24_hours()` ile AYNI 1:1
        davranış (eksik saat -> `_synthetic_zero_demand_window`).
      - İLKBAHAR İLERİ ALMA (23 saat): 23 GERÇEK saat 1:1 + TEK bir
        `_synthetic_gap_window()` (var OLMAYAN yerel saat, sıfır talep,
        UYDURMA veri YOK) = 24.
      - SONBAHAR GERİ ALMA (25 saat): 24 saat 1:1 + tekrarlanan TEK
        yerel saat için 2 GERÇEK saat `_merge_duplicate_local_hour()`
        ile BİRLEŞTİRİLİR (hiçbiri silinmez, additive+worst-of reducer) = 24.

    Hangi saatin "gap" (İLKBAHAR) veya "duplicate" (SONBAHAR) olduğu
    HARDCODE bir tarih/ay kontrolüyle DEĞİL, `[grid_start, grid_end)`
    aralığındaki HER GERÇEK UTC saatinin `zoneinfo.astimezone()` ile
    yerel saate çevrilmesinden (UTC->local yönü HER ZAMAN well-defined,
    ASLA ambiguous/nonexistent olmaz) DOĞAL olarak ORTAYA ÇIKAR: yerel
    saat etiketleri (0..23) beklenen sırayla ilerlerken bir TEKRAR
    (sonbahar) veya bir SIÇRAMA (ilkbahar) görülür.

    Non-DST/normal timezone'larda (İstanbul dahil - 2016'dan beri kalıcı
    UTC+3, DST uygulamıyor) `grid_end - grid_start` HER ZAMAN 24 saattir,
    bu fonksiyon `_pad_series_to_24_hours()` ile TAM olarak AYNI sonucu
    üretir - davranış REGRESSION'SIZ.
    """
    real_hours = int(round((grid_end - grid_start).total_seconds() / 3600))
    by_utc_start = {w["window_start"]: w for w in windows}

    def _entry_for(utc_hour: datetime) -> dict:
        key = utc_hour.isoformat()
        if key in by_utc_start:
            return by_utc_start[key]
        return _synthetic_zero_demand_window(utc_hour, utc_hour + timedelta(hours=1), tz)

    local_midnight_naive = day_start.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)

    display: list[dict] = []
    expected_label = 0
    i = 0
    while i < real_hours and expected_label < 24:
        utc_hour = grid_start + timedelta(hours=i)
        local_label = utc_hour.replace(tzinfo=timezone.utc).astimezone(tz).hour

        if local_label == expected_label:
            display.append(_entry_for(utc_hour))
            expected_label += 1
            i += 1
            continue

        if display and local_label == (expected_label - 1) % 24:
            # SONBAHAR - bu gerçek saat, HEMEN ÖNCE eklenen bucket ile
            # AYNI yerel saati taşıyor: iki gerçek pencere BİRLEŞİR.
            previous = display.pop()
            display.append(_merge_duplicate_local_hour(previous, _entry_for(utc_hour)))
            i += 1
            continue

        # İLKBAHAR - beklenen yerel saat hiç GERÇEKLEŞMEDİ (yerel saat
        # ileri sıçradı): sentetik gap bucket'ı eklenir. `i` İLERLEMEZ -
        # bu GERÇEK saat henüz tüketilmedi, bir sonraki turda (artık
        # `expected_label`'e eşit olması beklenir) işlenir.
        gap_local_start = local_midnight_naive + timedelta(hours=expected_label)
        # ADIM (Gap Bucket Key Collision Fix) - `utc_hour`'un KENDİSİ bir
        # sonraki turda AYNEN bu değerle GERÇEK bir bucket olarak da
        # eklenecek (henüz `i` İLERLEMEDİ) - gap'e transition anının TAM
        # KENDİSİNİ vermek, listedeki İKİ AYRI bucket'ın (sentetik gap +
        # onu izleyen gerçek saat) AYNI `window_start` anahtarını
        # taşımasına yol açardı ("no duplicate display hour" ihlali).
        # 1 mikrosaniye ÖNCESİ (bu projede daha önce AYNI sınıf çakışma
        # için kullanılan teknik) sıfır-genişlik semantiğini KORUR
        # (hâlâ `window_end`'e eşit, hâlâ hiçbir gerçek süre kapsamıyor)
        # ama benzersiz/kesinlikle-önce sıralanan bir anahtar verir.
        transition = utc_hour - timedelta(microseconds=1)
        display.append(_synthetic_gap_window(
            transition, gap_local_start, gap_local_start + timedelta(hours=1),
        ))
        expected_label += 1

    # Günün SON saati gap ise (nadir - beklenen yerel saat günün en
    # sonunda hiç gerçekleşmedi) kalan slot(lar) sentetik gap ile
    # tamamlanır.
    while expected_label < 24:
        transition = grid_start + timedelta(hours=real_hours) - timedelta(microseconds=1)
        gap_local_start = local_midnight_naive + timedelta(hours=expected_label)
        display.append(_synthetic_gap_window(
            transition, gap_local_start, gap_local_start + timedelta(hours=1),
        ))
        expected_label += 1

    return display


def _pad_series_to_24_hours(windows: list[dict], day_start: datetime, tz=None) -> list[dict]:
    """
    ADIM (24-Hour Graph) - Bölüm A/B/C/D: bir sürecin `windows`
    listesini, havalimanının YEREL operasyonel gününün (`day_start`
    - `operational_day_window()`'dan, engine.py/run_date_shift_replay.py
    ile AYNI production fonksiyonu) TAM 24 saatine (`day_start`,
    `day_start+1h`, ..., `day_start+23h`) tamamlar.

    KATKISAL/YIKICI OLMAYAN (additive) davranış: bu fonksiyon `windows`
    listesindeki HİÇBİR satırı SİLMEZ/ÜZERİNE YAZMAZ - `day_start`'ın
    dışında kalan (ör. başka bir güne ait, gerçek geçmiş) satırlar
    AYNEN listede kalır. SADECE `day_start..day_start+24h` aralığında
    eksik olan saatler `_synthetic_zero_demand_window()` ile EKLENİR.
    Bu ayrım kritik: `now` (dolayısıyla `day_start`) test/production
    çağrısına göre GERÇEK flight verisinden BAĞIMSIZ bir güne denk
    gelebilir (ör. `now` verilmeden gerçek duvar-saati kullanıldığında)
    - böyle bir durumda bile mevcut GERÇEK pencereler ASLA kaybolmaz,
    sadece o günün 24 saati de listeye eklenir (bkz. regresyon:
    `tests/test_30min_source_refresh_e2e.py`).

    Cross-day event güvenliği (Bölüm D): bu fonksiyon `window_start`
    değerlerini DEĞİŞTİRMEZ/KAYDIRMAZ - gerçek satırlar zaten
    `effective_time()`'ın ürettiği (departure -120dk / arrival +15dk)
    DOĞRU UTC saatine sahiptir (bkz. engine.py `floor_to_window`);
    burada SADECE `day_start..day_start+24h` aralığındaki 24 SLOT'un
    HANGİLERİNİN dolu olduğuna bakılır - bir gece yarısını aşan event
    zaten kendi doğru (önceki/sonraki güne taşmış olabilecek) saatinde
    durur, bu fonksiyon onu YANLIŞ bir güne TAŞIMAZ.
    """
    by_start = {w["window_start"]: w for w in windows}
    for k in range(24):
        start = day_start + timedelta(hours=k)
        end = start + timedelta(hours=1)
        key = start.isoformat()
        if key not in by_start:
            by_start[key] = _synthetic_zero_demand_window(start, end, tz)
    return sorted(by_start.values(), key=lambda w: w["window_start"])


def _resolve_airport_tz_and_day_start(session, airport_iata: str, now: datetime):
    """
    ADIM (24-Hour Graph, genişletildi: Timezone Display) - havalimanının
    GERÇEK `Airport.timezone`'undan (bilinmiyorsa/`zoneinfo`'da
    tanınmıyorsa hepsi None - UYDURMA bir UTC varsayımı/offset
    ÜRETİLMEZ, bkz. `resolve_airport_timezone` docstring'i) çözülen
    `(tz, day_start, day_end)` üçlüsü döner:

      - `tz`        : `window_start_local` üretimi için (bkz.
                       `_to_local_iso`) - Bölüm 2/3.
      - `day_start` : havalimanının GERÇEK yerel gece yarısının UTC
                       karşılığı (`operational_day_window()`'dan,
                       DEĞİŞMEDİ).
      - `day_end`   : bir SONRAKİ GERÇEK yerel gece yarısının UTC
                       karşılığı - ADIM (Calculation Horizon != Display
                       Horizon): `day_end - day_start` DST geçiş
                       günlerinde 24 saat OLMAK ZORUNDA DEĞİLDİR (23
                       saat ilkbahar ileri alma / 25 saat sonbahar geri
                       alma günlerinde) - bu, `_pad_series_to_24_hours`'ın
                       ESKİ, `_day_end`'i atıp `day_start + 24h` sabit
                       ızgarasını KULLANAN davranışının kökündeki DST
                       bug'ıydı (bkz. rapor). Artık ÇAĞIRAN TARAF (bkz.
                       `process_series`) bu gerçek sınırı kullanıp DISPLAY
                       ızgarasını GERÇEK yerel gün uzunluğuna göre kurar,
                       eksik/fazla saati (gap/duplicate) deterministic
                       olarak Bölüm 4 kuralıyla 24 GÖRÜNÜR bucket'a
                       projekte eder - `day_end`'in KENDİSİ hâlâ SADECE
                       görünür grafiğin sınırıdır, `QueuePrediction`
                       hesaplama/persistence ufkunu (calculation state)
                       HİÇ ETKİLEMEZ/KISALTMAZ (bkz. engine.py - orada
                       hiçbir 24 saatlik/gece yarısı truncation YOK).

    `tz` None dönerse çağıran taraf PADDING YAPMAZ VE local alanları
    `None` bırakır - "her zaman 24 saat/local label" garantisi, güvenilir
    bir yerel gün sınırı KURULABİLEN havalimanları için geçerlidir
    (production'daki TÜM gerçek havalimanları zaten `flight_airports.
    sql`'den bir timezone taşır; sınırlama SADECE bu alanın gerçekten
    boş/tanınmayan olduğu nadir durum için not edilir, gizlenmez).
    """
    airport = session.get(Airport, airport_iata)
    if airport is None:
        return None, None, None
    tz = resolve_airport_timezone(airport.timezone)
    if tz is None:
        return None, None, None
    day_start, day_end = operational_day_window(tz, now)
    return tz, day_start, day_end


def _pick_current(rows: list[QueuePrediction], now: datetime) -> QueuePrediction | None:
    """
    ADIM 6A-UI düzeltmesi - "current" artık `max(window_start)` DEĞİL.

    ESKİ davranış (ADIM 5H/6A) tam-günlük bir fixture'da günün EN SON
    penceresini (örn. akşam 20:45) "current" seçiyordu - inşa edilen
    08:00-08:30 surge'ünden tamamen habersiz, yanıltıcı bir sonuçtu
    (bkz. ADIM 6A raporu, bulgu V.1).

    Doğru semantik: `now`'ı GERÇEKTEN kapsayan pencere (`window_start
    <= now < window_end`). Kapsayan pencere yoksa - sık görülen bir
    durum, çünkü pencereler arasında uçuşsuz boşluklar olabilir (bkz.
    `window_starts()` - boş pencereye satır YAZILMAZ) - en yakın
    GEÇMİŞ pencere kullanılır ("son bilinen durum"); geçmişte hiç
    pencere yoksa (tüm veri gelecekte, örn. henüz hiç uçuş
    gerçekleşmemiş çok erken bir refresh) en yakın GELECEK pencere
    kullanılır. `rows` zaten `window_start`'a göre sıralı geldiği için
    (bkz. `process_series`) hem "en yakın geçmiş" hem "en yakın
    gelecek" O(n) tek geçişte bulunur.
    """
    if not rows:
        return None

    containing = [r for r in rows if r.window_start <= now < r.window_end]
    if containing:
        return containing[-1]

    past = [r for r in rows if r.window_start <= now]
    if past:
        return past[-1]

    return rows[0]


def process_series(
    session,
    airport_iata: str,
    process: str,
    since: datetime | None = None,
    now: datetime | None = None,
    day_start: datetime | None = None,
    tz=None,
    day_end: datetime | None = None,
) -> dict:
    """
    Bir (havalimanı, süreç) çifti için kronolojik pencere serisi +
    "current" (şu anki pencere) durumu.

    `now` verilmezse gerçek UTC "şimdi" kullanılır; testler
    DETERMİNİSTİK bir `now` geçebilir (bkz. `_pick_current`).

    Pencere yoksa (bu havalimanı/süreç için hiç tahmin üretilmemiş)
    `current: None, windows: []` döner - bu durum çağıran tarafta
    ASLA "NORMAL" ile karıştırılmamalı.

    day_start/day_end : ADIM (Calculation Horizon != Display Horizon) -
                verilirse (havalimanının çözülebilen bir timezone'u
                varsa, bkz. `_resolve_airport_tz_and_day_start`)
                `windows` GERÇEK yerel gün sınırına (`_build_exact24_
                display_windows()` - DST-farkında, `day_end - day_start`
                23/24/25 saat olabilir) göre tam 24 GÖRÜNÜR bucket'a
                projekte edilir ve `current` bu PROJEKTE EDİLMİŞ liste
                üzerinden (`_pick_current_window` - `_pick_current` ile
                AYNI üç kollu mantık, ama dict listesi üzerinde) seçilir.
                `day_end` verilmezse (None, varsayılan - eski
                çağıranlar/testler) `day_start + 24h` sabit ızgarasına
                GERİ DÜŞÜLÜR (DST-farkında olmayan, ESKİ davranış) -
                DOĞRUDAN geriye dönük uyumluluk için, YENİ hiçbir
                çağıran bunu KULLANMAMALI. `day_start`/`tz` hiçbiri
                verilmezse (None) ESKİ davranış (sadece gerçek satırlar,
                padding YOK) birebir korunur - doğrudan çağıranlar/eski
                testler ETKİLENMEZ.
    tz        : ADIM (24-Hour Graph Timezone Display) - `day_start` ile
                AYNI kaynaktan (`_resolve_airport_tz_and_day_start`)
                gelir; HER pencereye (gerçek VEYA padded, current dahil)
                `window_start_local`/`window_end_local` eklemek için
                `_window_to_dict`/`_synthetic_zero_demand_window`'a
                AYNEN iletilir. `internal UTC eşleştirme` (`by_start`
                anahtarı hâlâ `window_start` - CANONICAL UTC) bundan
                HİÇ etkilenmez (Bölüm 8) - sadece SERİLEŞTİRME/görüntü
                alanı eklenir.
    """
    now = now if now is not None else _utcnow()

    query = select(QueuePrediction).where(
        QueuePrediction.airport_iata == airport_iata,
        QueuePrediction.process == process,
    )
    if since is not None:
        query = query.where(QueuePrediction.window_start >= since)
    query = query.order_by(QueuePrediction.window_start)

    rows = session.execute(query).scalars().all()

    if day_start is not None:
        # ADIM (Current Operational Day Isolation) - BUG FIX: `rows` yukarıda
        # bu havalimanı/süreç için VERİTABANINDAKİ TÜM `QueuePrediction`
        # satırlarını (önceki operasyonel günler dahil, HİÇBİR tarih filtresi
        # olmadan) çeker - persistan/çok-günlü bir DB'de (ör. 19 Eylül + 20
        # Eylül aynı dosyada) bu, `windows`'un birden fazla güne ait GERÇEK
        # satırları KARIŞIK içermesine yol açıyordu (bkz. rapor - stale-day
        # leak bulgusu).
        #
        # ADIM (Calculation Horizon != Display Horizon - Bölüm 3/5,
        # DÜZELTME): `QueuePrediction.operational_date` eşitliğini bir
        # DISPLAY filtresi olarak KULLANMAKTAN vazgeçildi - bu ESKİ
        # yaklaşım "backlog GÜNLERCE ileri taşınabilir" (Bölüm 9 - passport
        # departure completion_time'ı security_intl'in arrival_time'ı olur,
        # ağır backlog'ta bu SONRAKİ takvim gününe geçebilir) davranışıyla
        # ÇATIŞIYORDU: bir satır Day-1'in flight-run'ından ÜRETİLDİĞİ için
        # `operational_date=Day-1` taşır, ama `window_start`'ı GERÇEKTEN
        # Day-2'nin saat aralığına düşer. Bu satır `_within_display_bounds`
        # ile Day-1'in ızgarasından (day_end'in dışında kaldığı için, doğru
        # şekilde) ELENİYORDU; AMA `operational_date == Day-1 != Day-2`
        # olduğu için Day-2'nin ızgarasına da hiç GİRMİYORDU - satır
        # HİÇBİR grafikte görünmeyen bir "yetim" oluyordu (Bölüm 5'in
        # yasakladığı TAM senaryo: "queue midnight'ta sıfırlanmış gibi
        # davranmamalı").
        #
        # Artık TEK doğruluk kaynağı `window_start`'ın GERÇEK yerel gün
        # sınırına (`grid_start`/`grid_end` - aşağıda, DST-farkında
        # `day_end`'den türetilir) göre KONUMUdur - hangi flight-run'ın
        # o satırı ÜRETTİĞİ (operational_date) DEĞİL. Bu artık GÜVENLİDİR
        # (eski "stale-day leak" riski YOK): `grid_start`/`grid_end` artık
        # `day_start + sabit 24h` YAKLAŞIKLIĞI değil, GERÇEK yerel gün
        # sınırıdır (bu ADIM'ın kendisi) - bu yüzden `window_start` aralığı
        # TEK BAŞINA hem Bölüm 61'in sınır-geçişli spillover'ını (Day-1'in
        # kendi backlog'u Day-1'in ızgarasında kalırken) hem de Bölüm 3/5'in
        # istediği Day-2 görünürlüğünü (Day-1-etiketli ama Day-2 saatine
        # düşen satır artık Day-2'nin ızgarasında GÖRÜNÜR) doğru ayırır -
        # `operational_date` etiketine ARTIK gerek yok (persistence/
        # retention'daki KENDİ rolü DEĞİŞMEDİ, SADECE bu display filtresi
        # olarak kullanılmasından vazgeçildi).
        #
        # ADIM (Exact 24-Bucket Visible Graph) - Bölüm 3.3: CALCULATION
        # STATE != VISIBLE GRAPH RANGE. Görünür grafik penceresi KESİN
        # OLARAK `[grid_start, grid_end)` ile sınırlanır - margin/tolerans
        # YOK ("24 + legitimate overflow KABUL EDİLMEZ"). Backlog'un
        # KENDİSİ (event simülasyonu, wait, risk - `calculation state`)
        # HİÇ DEĞİŞMEDİ - `current` göstergesi GERÇEK, güncel backlog
        # şiddetini TAŞIMAYA DEVAM EDER. SADECE bugünün 24 saatlik
        # x-ekseninin DIŞINDAKİ satırlar bu günün grafiğinde AYRI bir
        # bucket olarak LİSTELENMEZ - bir SONRAKİ günün kendi grafiği
        # istendiğinde ise (yukarıdaki düzeltme sayesinde) ARTIK KAYBOLMAZ.
        #
        # ADIM (Half-Hour-Offset Timezone Grid Fix) - gerçek `data/*.json`
        # ile 78 havalimanı üzerinde uçtan uca replay testi DEL (Asia/
        # Kolkata, UTC+5:30) ve KBL (Asia/Kabul, UTC+4:30) icin 25 bucket
        # ürettiğini ortaya çıkardı: `window_start` değerleri HER ZAMAN
        # tam UTC saatine floor edilir (`floor_to_window()` - queue
        # math'in KENDİSİ, DEĞİŞTİRİLMEDİ), ama yarım-saat offsetli bir
        # timezone'da `day_start` (yerel gece yarısının UTC karşılığı)
        # ":30" üzerinde durur - `_pad_series_to_24_hours`'ın `day_start +
        # k*1h` ızgarası bu yüzden GERÇEK satırların ":00" damgasıyla HİÇ
        # ÇAKIŞMAZ, 24 sentetik sıfır-talep slotu + kendi ayrı anahtarına
        # düşen gerçek satır(lar) üst üste binip 24'ü AŞAR.
        #
        # Düzeltme SADECE bu GÖRÜNÜR ızgarayı (`grid_start`/`grid_end`)
        # bir sonraki tam UTC saatine yuvarlar - `day_start`'ın kendisi
        # (operational_date, timezone display, calculation state) HİÇ
        # DEĞİŞMEDİ. Yukarı (aşağı değil) yuvarlanır: aşağı yuvarlama
        # önceki yerel güne ait bir saati (ör. DEL için yerel 23:30) bu
        # günün ızgarasına SIZDIRIRDI; yukarı yuvarlama ızgarayı SIKI
        # ŞEKİLDE `[day_start, day_end)` içinde tutar (en fazla 59 dakikalık
        # bir başlangıç dilimi hiçbir tam-saat bucket'ına düşmez - floor_
        # to_window zaten bu dilimdeki satırları bir ÖNCEKİ UTC saatine
        # floor ettiği için bu satırlar `day_start`'ın altında kalıp
        # ZATEN dışlanıyordu, bu satırda YENİ bir dışlama YOK).
        grid_start = day_start
        if grid_start.minute or grid_start.second or grid_start.microsecond:
            grid_start = grid_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

        if day_end is not None:
            # ADIM (Calculation Horizon != Display Horizon - Bölüm 3/4):
            # `day_end` çağıran taraftan (`_resolve_airport_tz_and_day_
            # start`) GERÇEK yerel gün sınırı olarak gelir - DST geçiş
            # günlerinde `day_end - day_start` 23/25 saat olabilir (ESKİ
            # `grid_start + 24h` sabit ızgarası BUNU YOK SAYIYORDU, bkz.
            # rapor). `grid_end` de `grid_start` ile AYNI yarım-saat-
            # offset yuvarlamasına tabi tutulur (Half-Hour-Offset Timezone
            # Grid Fix ile TUTARLI - ikisi de aynı miktarda ileri kayar).
            grid_end = day_end
            if grid_end.minute or grid_end.second or grid_end.microsecond:
                grid_end = grid_end.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            # `day_end` verilmeyen (ESKİ, doğrudan) çağıranlar için
            # GERİYE DÖNÜK UYUMLU sabit-24-saat ızgara - YENİ hiçbir
            # çağıran bunu kullanmamalı (bkz. `process_series` docstring'i).
            grid_end = grid_start + timedelta(hours=24)

        def _within_display_bounds(row: QueuePrediction) -> bool:
            return grid_start <= row.window_start < grid_end

        rows_for_windows = [r for r in rows if _within_display_bounds(r)]
        windows = [_window_to_dict(row, tz) for row in rows_for_windows]
        windows = _build_exact24_display_windows(windows, grid_start, grid_end, day_start, tz)
        current = _pick_current_window(windows, now)
    else:
        windows = [_window_to_dict(row, tz) for row in rows]
        current_row = _pick_current(rows, now)
        current = _window_to_dict(current_row, tz) if current_row is not None else None

    return {
        "process": process,
        "current": current,
        "windows": windows,
    }


def _pick_current_window(windows: list[dict], now: datetime) -> dict | None:
    """
    BUG-02 düzeltmesi - `_pick_current()` ile AYNI üç kollu mantık
    (kapsayan / en yakın geçmiş / en yakın gelecek), ama BURADA zaten
    AYNI `window_start`'a göre birleştirilmiş `windows` listesi
    üzerinde çalışır. `windows` `sorted(by_window)`'dan geldiği için
    (bkz. `_merge_overall_series`) hâlâ kronolojik sıralı.
    """
    if not windows:
        return None

    containing = [
        w for w in windows
        if datetime.fromisoformat(w["window_start"]) <= now < datetime.fromisoformat(w["window_end"])
    ]
    if containing:
        return containing[-1]

    past = [w for w in windows if datetime.fromisoformat(w["window_start"]) <= now]
    if past:
        return past[-1]

    return windows[0]


def _merge_overall_series(*process_results: dict, now: datetime) -> dict:
    """
    ADIM G - GENEL HAVALİMANI YOĞUNLUĞU serisi. Verilen süreç
    sonuçlarının (`process_series()` çıktıları - ör. domestic_security,
    international_security, passport) `windows`'larını AYNI
    `window_start`'a göre birleştirip HER pencerede EN YÜKSEK severity'yi
    (`RISK_ORDER`) seçer. YENİ bir risk/skor formülü YOK - sadece zaten
    hesaplanmış `risk` değerleri arasında MAX.

    ADIM 6D-2 H2 / ADIM (Overall Tie-Break = Highest Wait) - wait
    KAYNAĞI iki adımlı seçilir (risk matematiği bu adımda DEĞİŞMEDİ,
    sadece wait'in HANGİ sürecin satırından taşınacağı düzeltildi):

      1) Önce en yüksek severity bulunur (yukarıdaki risk seçimiyle
         AYNI `RISK_ORDER`).
      2) O severity'yi taşıyan süreçler arasında (birden fazla olabilir -
         ör. security_intl VE passport aynı anda CRITICAL) artık gerçek/
         finite bir `estimated_wait_minutes`'ı OLAN, o wait'i EN YÜKSEK
         olan süreç tercih edilir (ör. CRITICAL/57, CRITICAL/146,
         CRITICAL/171 -> 171 kazanır); hiçbirinde finite wait yoksa
         (hepsi security gibi wait modelsizse) `estimated_wait_minutes
         = None` kalır. ESKİDEN (Graph Bug #2) burada "argüman sırasına
         göre İLK süreç" seçiliyordu - bu, `_merge_overall_series`'e
         VERİLME SIRASINA (domestic_security, international_security,
         passport_departure, passport_arrival) bağlı, GERÇEK wait
         büyüklüğüyle İLGİSİZ bir sonuç üretiyordu (bkz. rapor - IST
         16:00->17:00 local geçişinde overall'ın 136.7'den 57.5'e
         "düşmesi", oysa o saatte international_security 146.7 VE
         arrival_passport 171.5 idi - her ikisi de domestic_security'nin
         57.5'inden YÜKSEKTİ). Artık tie-break DAİMA gerçek en yüksek
         wait'i taşıyan süreci seçer - argüman sırası SONUCU
         ETKİLEMEZ.

    Böylece DÜŞÜK severity'deki bir sürecin wait'i overall'a HİÇBİR
    ZAMAN taşınmaz (ör. security_intl=CRITICAL/wait=None VE
    passport=HIGH/wait=40 iken overall CRITICAL/None döner - HIGH'ın
    wait'i "ödünç" alınmaz). Kazanan pencerenin KENDİ
    `estimated_wait_minutes`'ı OLDUĞU GİBİ taşınır; ayrı bir "overall
    wait" ORTALAMASI ASLA hesaplanmaz (birden fazla sürecin wait'i
    toplanıp/ortalanıp YENİ bir sayı üretilmez). Aynı `_worst_of()`
    hem `windows` serisi hem `current` için kullanılır - ikisi arasında
    FARKLI bir tie-break kuralı YOK.

    Bir pencerede sadece TEK bir süreçten veri varsa (ör. o saatte
    sadece domestic kalkış oldu) o sürecin riski/wait'i AYNEN kullanılır.
    """
    by_window: dict[str, list[dict]] = {}
    for result in process_results:
        for window in result["windows"]:
            by_window.setdefault(window["window_start"], []).append(window)

    def _worst_of(entries: list[dict]) -> dict:
        base = _pick_highest_severity_entry(entries)
        wait = base["estimated_wait_minutes"]
        return {
            "window_start": base["window_start"],
            "window_end": base["window_end"],
            # ADIM (24-Hour Graph Timezone Display) - Bölüm 5: Overall da
            # görünür 5 grafikten biri - `base` (bir alt-sürecin ZATEN
            # tz-aware `_window_to_dict`/`_synthetic_zero_demand_window`
            # çıktısı) hangi local alanları taşıyorsa AYNEN kopyalanır
            # (tüm alt-süreçler AYNI havalimanı/AYNI tz'den geldiği için
            # hepsinin local değeri zaten özdeştir).
            "window_start_local": base.get("window_start_local"),
            "window_end_local": base.get("window_end_local"),
            "risk": base["risk"],
            "risk_label": base["risk_label"],
            "estimated_wait_minutes": wait,
        }

    windows = [_worst_of(by_window[start]) for start in sorted(by_window)]

    # BUG-02 düzeltmesi - `current` artık her sürecin KENDİ (farklı
    # saatlere düşebilen) fallback current'ından DEĞİL, zaten aynı
    # `window_start`'a göre birleştirilmiş `windows` listesinden
    # `_pick_current`'la AYNI üç kollu mantıkla seçilir. Böylece
    # farklı saatlerdeki süreç current'ları asla birbiriyle severity
    # karşılaştırmasına girmez - sadece `now`'ı kapsayan (veya en
    # yakın) TEK saatin zaten birleştirilmiş sonucu kullanılır.
    current = _pick_current_window(windows, now)

    return {"process": "overall", "current": current, "windows": windows}


def _international_departure_split(
    passport_departure: dict, international_security: dict,
) -> dict:
    """
    ADIM (International Departure Split Graphs) - Bölüm 9-12: passport
    ve security artık İKİ AYRI zaman serisi/grafik olarak sunulur, TEK
    bir "worst-of" pencereye ZORLANMAZ.

    Gerekçe (ZRH regresyonu): passport'un GERÇEKTEN doldurduğu backlog,
    security'ye saatler SONRA (kendi `completion_time`'ı - bkz.
    `engine.py:_event_driven_queue_demand`) ulaşabilir. Eski birleşik
    "current" tek bir `window_start` seçtiği için (worst-of), passport
    06:00'da 66.8 dk gösterirken security'nin current'ı 08:00'a
    düşünce passport'un KENDİ current'ı sahte biçimde "—" görünebiliyordu
    (iki fiziksel aşama birbirinin `current` seçimini EZİYORDU). Artık
    her biri KENDİ `process_series()` sonucunu (kendi `current`/
    `windows`, kendi `_pick_current` üç kollu mantığı) AYNEN taşır -
    YENİ bir risk/wait hesabı YOK, sadece TEK "worst-of" birleştirme
    ADIMI kaldırıldı.
    """
    return {
        "process": "international_departure",
        "passport": passport_departure,
        "security": international_security,
    }


def airport_predictions(
    session, airport_iata: str, since: datetime | None = None, now: datetime | None = None
) -> dict:
    """
    Bir havalimanının süreç serileri + genel durum - frontend'in tek
    çağrısı (`GET /api/airports/{iata}/predictions`'ın gövdesi).

    ADIM 6A: `overall` alanı EKLENDİ. ADIM (Security Domestic/
    International Split) + ADIM G: `domestic_security`/
    `international_security`/`international_passport` alanları EKLENDİ;
    `security`/`passport` (ESKİ, birleşik) alanları KALDIRILMADI -
    geriye dönük uyumlu. `overall` (ADIM G) domestic_security +
    international_security + passport (birleşik) üzerinden hesaplanır -
    `overall_status()` fonksiyonunun kendisi hâlâ genel/2-argümanlı
    kullanılabilir (geriye dönük), sadece BURADAKİ çağıran taraf 3 girdi
    kullanıyor.

    ADIM (4-Graph API Contract) - Bölüm 15/26/35/46: `domestic_security`,
    `international_departure`, `international_arrival` (SADECE
    varış-kökenli passport) - TAM 4 grafik contract'ı. `overall`
    DEĞİŞTİRİLMEDİ (Bölüm 35: fiziksel YENİ bir queue yaratmaz, zaten
    var olan `domestic_security`/`international_security`/
    `passport_departure`/`passport_arrival` sonuçlarının özeti olarak
    KALDI).

    ADIM (International Departure Split Graphs): `international_departure`
    ARTIK "worst-of" tek bir birleşik pencere DEĞİL -
    `{"process", "passport", "security"}` şekli, `passport` =
    `PROCESS_PASSPORT_DEPARTURE`'ın kendi `process_series()`'i, `security`
    = `PROCESS_SECURITY_INTL`'in kendi `process_series()`'i (bkz.
    `_international_departure_split`). Her ikisi de KENDİ `current`/
    `windows`'unu, KENDİ `_pick_current` üç kollu mantığıyla taşır -
    security'nin current'ı passport'unkini ASLA EZMEZ (ZRH regresyonu).

    ADIM 6A-UI: `now` BİR KEZ hesaplanıp TÜM süreçlere AYNI değer
    geçirilir - ayrı ayrı "gerçek an"ı sorgulamak, aralarındaki
    milisaniyelik gecikmenin bir pencere sınırını geçip süreçlerin
    FARKLI anlara göre "current" seçmesine yol açabilirdi.

    ADIM (24-Hour Graph) - Bölüm A/B/C: kullanıcı-visible BEŞ grafik
    (overall/domestic_security/international_departure'ın passport VE
    security'si/international_arrival) artık HER ZAMAN TAM 24 saat
    gösterir - `day_start` (havalimanının GERÇEK timezone'undan,
    `_resolve_airport_tz_and_day_start`) çözülebiliyorsa bu dört gerçek
    süreç PADDED olarak hesaplanır; `overall`/`international_departure`
    zaten bunların ÜZERİNE inşa edildiği için (bkz. `_merge_overall_
    series`/`_international_departure_split`) ONLAR DA otomatik olarak
    24 saat olur - YENİ bir birleştirme mantığı İCAT EDİLMEDİ. Legacy
    `security`/`passport` (geriye dönük uyumluluk alanları) BİLİNÇLİ
    OLARAK PAD EDİLMEDİ - eski davranışları birebir korunur.

    ADIM (24-Hour Graph Timezone Display): AYNI `tz` çözümü, `window_
    start_local`/`window_end_local` üretmek için BEŞ görünür grafiğin
    TAMAMINA (bkz. `process_series`'in `tz` parametresi) iletilir.
    `window_start`/`window_end` (CANONICAL UTC) HİÇ DEĞİŞMEDİ - sadece
    EK bir görüntü alanı eklendi (Bölüm 3/14 - backward compatible).
    """
    now = now if now is not None else _utcnow()
    tz, day_start, day_end = _resolve_airport_tz_and_day_start(session, airport_iata, now)

    security = process_series(session, airport_iata, PROCESS_SECURITY, since, now)
    passport = process_series(session, airport_iata, PROCESS_PASSPORT, since, now)
    domestic_security = process_series(session, airport_iata, PROCESS_SECURITY_DOMESTIC, since, now, day_start, tz, day_end)
    international_security = process_series(session, airport_iata, PROCESS_SECURITY_INTL, since, now, day_start, tz, day_end)
    passport_departure = process_series(session, airport_iata, PROCESS_PASSPORT_DEPARTURE, since, now, day_start, tz, day_end)
    passport_arrival = process_series(session, airport_iata, PROCESS_PASSPORT_ARRIVAL, since, now, day_start, tz, day_end)

    # ADIM (Overall Graph Legacy Passport Bug Fix): Overall ARTIK
    # legacy/8-server `passport` (PROCESS_PASSPORT) serisini KULLANMIYOR -
    # scale-derived `passport_departure`/`passport_arrival`'ı kullanıyor.
    # Gerekçe: `passport` hâlâ eski `passport_effective_server_count()`
    # (4x2=8, Airport.scale'den HABERSİZ) formülüyle hesaplanıyor - bir
    # large havalimanında (20/30 server) Overall'ın risk/wait'i bu YANLIŞ
    # referansa göre çıkıyordu, International Departure/Arrival (zaten
    # passport_departure/passport_arrival kullanıyordu) ile TUTARSIZ
    # olabiliyordu. `passport`/`international_passport` legacy API
    # alanları DEĞİŞMEDİ (geriye dönük uyumluluk), SADECE overall'ın
    # girdisi değişti.
    overall = _merge_overall_series(
        domestic_security, international_security,
        passport_departure, passport_arrival, now=now,
    )
    # ADIM (International Departure Split Graphs) - Bölüm 9-12: artık
    # TEK birleşik ("worst-of") pencere DEĞİL, passport/security kendi
    # BAĞIMSIZ current/windows serisini taşıyor (bkz.
    # `_international_departure_split` docstring'i - ZRH regresyonu).
    international_departure = _international_departure_split(
        passport_departure, international_security,
    )

    return {
        "airport": airport_iata,
        "breakdown": traffic_breakdown(session, airport_iata, now),
        "domestic_security": domestic_security,
        "international_security": international_security,
        "international_passport": passport,
        "international_departure": international_departure,
        "international_arrival": passport_arrival,
        "overall": overall,
        "security": security,
        "passport": passport,
    }
