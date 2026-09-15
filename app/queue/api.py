"""
ADIM 5H - Minimal read-only sorgu katmanı (frontend/HTTP için).

Bu modül GÖRSELLEŞTİRME İÇİN veri hazırlar - hiçbir yoğunluk/kapasite
HESABI yapmaz. Erlang-C, passenger demand, effective_time, risk,
confidence, reasons - bunların TEK doğruluk kaynağı `engine.py` +
`core/scoring.py` + `reasons/detector.py`'dir; burada SADECE zaten
hesaplanmış `QueuePrediction` satırları okunup JSON-uyumlu sözlüklere
çevrilir.

`reporting.py` (AŞAMA 9) ile KARIŞTIRILMAMALI: o modül SAATLİK özet
üretir (birden fazla 15 dk penceresini bir saate toplar). Frontend
burada GERÇEK 15 dakikalık pencereleri, birbirine karıştırılmadan,
zaman serisi olarak görmek istiyor - bu yüzden ham `QueuePrediction`
satırları (opsiyonel bir `since` filtresiyle) doğrudan döner.
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
    PROCESS_SECURITY,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_ORDER,
    RISK_UNKNOWN,
)
from .models import Airport, Flight, QueuePrediction

# --- Risk -> UI durum etiketi (TEK yer, SADECE sunum) ------------------
#
# Backend'in ürettiği risk değeri (LOW/MEDIUM/HIGH/CRITICAL/UNKNOWN)
# BURADA değişmez - sadece kullanıcıya gösterilecek Türkçe etikete
# eşlenir. UI'da yalnızca 3 resmi durum var (NORMAL/YOĞUN/ÇOK YOĞUN);
# backend'in 5 değeri bu 3'e sıkıştırılırken:
#   - HIGH ve CRITICAL ikisi de "ÇOK YOĞUN" gösterilir (ham `risk`
#     alanı yanıtta AYNEN de bulunur - frontend istersen ikisini görsel
#     olarak ayırabilir, bilgi kaybı yok, sadece etiket ortak).
#   - UNKNOWN "BİLİNMİYOR" olur - "NORMAL"a KESİNLİKLE düşürülmez
#     (bkz. modül testleri) - baseline eksikliğini normal yoğunlukla
#     karıştırmak, ADIM 5H'nin en kritik kuralı.
RISK_TO_UI_LABEL = {
    RISK_LOW: "NORMAL",
    RISK_MEDIUM: "YOĞUN",
    RISK_HIGH: "ÇOK YOĞUN",
    RISK_CRITICAL: "ÇOK YOĞUN",
    RISK_UNKNOWN: "BİLİNMİYOR",
}


def ui_label_for_risk(risk: str | None) -> str | None:
    """
    Ham backend risk değerini UI etiketine çevirir.

    `risk is None` (bu havalimanı/süreç için HİÇ pencere yok) `None`
    döner - çağıran taraf (frontend) bunu "Veri bulunamadı" ile
    göstermeli, "NORMAL" ile DEĞİL. Bilinmeyen bir risk string'i
    gelirse (ileride yeni bir değer eklenirse) olduğu gibi geçirilir -
    sessizce NORMAL'e düşürülmez.
    """
    if risk is None:
        return None
    return RISK_TO_UI_LABEL.get(risk, risk)


def overall_status(security_risk: str | None, passport_risk: str | None) -> dict:
    """
    ADIM 6A §13-16 - GENEL havalimanı yoğunluğu. SADECE presentation/
    aggregation - hiçbir YENİ risk hesabı yapmaz, security/passport
    formüllerine dokunmaz.

    Kural: iki sürecin CURRENT riskinden EN YÜKSEK severity kazanır
    (`RISK_ORDER` - AŞAMA 9'da "saatin en kötü penceresi" için
    kullanılan AYNI sıralama, burada TEKRAR TANIMLANMADI):

        LOW < MEDIUM < HIGH < CRITICAL

    UNKNOWN bir "gerçek risk" DEĞİLDİR - `RISK_ORDER`'da en altta
    durur, bu yüzden diğer taraf gerçek bir risk taşıyorsa asla onu
    ezmez (`HIGH + UNKNOWN -> HIGH`). Sadece HER İKİ taraf da UNKNOWN
    (veya biri UNKNOWN biri no-data) olduğunda sonuç UNKNOWN'dır
    (`UNKNOWN + UNKNOWN -> UNKNOWN`, label "BİLİNMİYOR").

    Girdilerin İKİSİ de `None` ise (bu havalimanı için security VE
    passport'un HİÇBİRİNDE satır yok) `{"risk": None, "label": None}`
    döner - ADIM 5H'nin "no-data ASLA NORMAL değildir, hatta
    BİLİNMİYOR ile de karıştırılmaz" ayrımı overall'da da korunur;
    frontend bunu "Veri bulunamadı" ile gösterir.

    Sadece `security.current`/`passport.current` (ŞU ANKİ pencere)
    kullanılır - serideki geçmiş/gelecek pencerelerin maksimumu
    KULLANILMAZ (bir havalimanı geçmişte bir yerde HIGH göstermiş
    diye sürekli HIGH görünmez).
    """
    candidates = [r for r in (security_risk, passport_risk) if r is not None]
    if not candidates:
        return {"risk": None, "label": None}

    worst = max(candidates, key=lambda r: RISK_ORDER.get(r, -1))
    return {"risk": worst, "label": ui_label_for_risk(worst)}


def tracked_airports(session) -> list[str]:
    """
    İzlenen havalimanları - `QueuePrediction`'da GERÇEKTEN tahmini
    olan havalimanlar (hiçbir hardcoded liste YOK). Alfabetik sıralı.

    GERİYE DÖNÜK UYUMLULUK: bu fonksiyonun/`/api/airports`'un dönüş
    şekli (düz string listesi) DEĞİŞTİRİLMEDİ - havalimanı adı
    isteyen tüketiciler `airport_directory()`/`/api/airports/directory`
    kullanmalı (ADIM 6A-UI-2).
    """
    rows = session.execute(
        select(QueuePrediction.airport_iata)
        .distinct()
        .order_by(QueuePrediction.airport_iata)
    ).scalars().all()
    return list(rows)


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


def _window_to_dict(row: QueuePrediction) -> dict:
    """
    Tek bir `QueuePrediction` satırının JSON-uyumlu görünümü.
    Hiçbir alan burada YENİDEN HESAPLANMAZ.
    """
    return {
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
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
) -> dict:
    """
    Bir (havalimanı, süreç) çifti için kronolojik pencere serisi +
    "current" (şu anki pencere) durumu.

    `now` verilmezse gerçek UTC "şimdi" kullanılır; testler
    DETERMİNİSTİK bir `now` geçebilir (bkz. `_pick_current`).

    Pencere yoksa (bu havalimanı/süreç için hiç tahmin üretilmemiş)
    `current: None, windows: []` döner - bu durum çağıran tarafta
    ASLA "NORMAL" ile karıştırılmamalı.
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
    windows = [_window_to_dict(row) for row in rows]

    current_row = _pick_current(rows, now)
    current = _window_to_dict(current_row) if current_row is not None else None

    return {
        "process": process,
        "current": current,
        "windows": windows,
    }


def airport_predictions(
    session, airport_iata: str, since: datetime | None = None, now: datetime | None = None
) -> dict:
    """
    Bir havalimanının security + passport serisi + genel durum -
    frontend'in tek çağrısı (`GET /api/airports/{iata}/predictions`'ın
    gövdesi).

    ADIM 6A: `overall` alanı EKLENDİ - mevcut `security`/`passport`
    alanları KALDIRILMADI/yeniden adlandırılmadı (geriye dönük uyumlu).

    ADIM 6A-UI: `now` BİR KEZ hesaplanıp security VE passport'a AYNI
    değer geçirilir - ikisi ayrı ayrı "gerçek an"ı sorgularsa, ikisi
    arasındaki milisaniyelik gecikme bir pencere sınırını geçip
    security/passport'un FARKLI anlara göre "current" seçmesine yol
    açabilirdi (nadir ama gerçek bir tutarsızlık riski).
    """
    now = now if now is not None else _utcnow()
    security = process_series(session, airport_iata, PROCESS_SECURITY, since, now)
    passport = process_series(session, airport_iata, PROCESS_PASSPORT, since, now)

    security_risk = security["current"]["risk"] if security["current"] else None
    passport_risk = passport["current"]["risk"] if passport["current"] else None

    return {
        "airport": airport_iata,
        "breakdown": traffic_breakdown(session, airport_iata, now),
        "overall": overall_status(security_risk, passport_risk),
        "security": security,
        "passport": passport,
    }
