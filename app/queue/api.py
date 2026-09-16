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

    ADIM 6D-2 H2 - wait KAYNAĞI iki adımlı seçilir (risk matematiği bu
    adımda DEĞİŞMEDİ, sadece wait'in HANGİ sürecin satırından
    taşınacağı düzeltildi):

      1) Önce en yüksek severity bulunur (yukarıdaki risk seçimiyle
         AYNI `RISK_ORDER`).
      2) O severity'yi taşıyan süreçler arasında (birden fazla olabilir -
         ör. security_intl VE passport aynı anda CRITICAL) gerçek/finite
         bir `estimated_wait_minutes`'ı OLAN ilk süreç tercih edilir;
         hiçbirinde yoksa (hepsi security gibi wait modelsizse)
         `estimated_wait_minutes = None` kalır.

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
        top_severity = max(RISK_ORDER.get(w["risk"], -1) for w in entries)
        top_entries = [w for w in entries if RISK_ORDER.get(w["risk"], -1) == top_severity]
        base = top_entries[0]
        wait = next(
            (w["estimated_wait_minutes"] for w in top_entries if w["estimated_wait_minutes"] is not None),
            None,
        )
        return {
            "window_start": base["window_start"],
            "window_end": base["window_end"],
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
    geriye dönük uyumlu. `overall` artık (ADIM G) domestic_security +
    international_security + passport üzerinden hesaplanıyor (eskiden
    birleşik security + passport'tu) - bu, "GENEL havalimanı yoğunluğu"
    tanımının ADIM G'de netleşen hâli; `overall_status()` fonksiyonunun
    kendisi hâlâ genel/2-argümanlı kullanılabilir (geriye dönük), sadece
    BURADAKİ çağıran taraf artık 3 girdi kullanıyor.

    ADIM 6A-UI: `now` BİR KEZ hesaplanıp TÜM süreçlere AYNI değer
    geçirilir - ayrı ayrı "gerçek an"ı sorgulamak, aralarındaki
    milisaniyelik gecikmenin bir pencere sınırını geçip süreçlerin
    FARKLI anlara göre "current" seçmesine yol açabilirdi.
    """
    now = now if now is not None else _utcnow()
    security = process_series(session, airport_iata, PROCESS_SECURITY, since, now)
    passport = process_series(session, airport_iata, PROCESS_PASSPORT, since, now)
    domestic_security = process_series(session, airport_iata, PROCESS_SECURITY_DOMESTIC, since, now)
    international_security = process_series(session, airport_iata, PROCESS_SECURITY_INTL, since, now)

    overall = _merge_overall_series(domestic_security, international_security, passport, now=now)

    return {
        "airport": airport_iata,
        "breakdown": traffic_breakdown(session, airport_iata, now),
        "domestic_security": domestic_security,
        "international_security": international_security,
        "international_passport": passport,
        "overall": overall,
        "security": security,
        "passport": passport,
    }
