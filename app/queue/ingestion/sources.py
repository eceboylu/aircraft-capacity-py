"""
AŞAMA 0 - Kaynak A ve Kaynak B'nin okunması ve normalize edilmesi.

Kaynak A : tarife verisi (arrivals / departures). Zaman, terminal,
           kapı, statü ve gecikme buradan gelir. aircraft_icao alanı
           çoğu kayıtta boştur.
Kaynak B : canlı uçuş verisi. aircraft_icao'nun asıl kaynağıdır.

Bu modül SAF ayrıştırma yapar: kayıt sözlüğünü alır, alanları
normalize eder, zamanları datetime'a çevirir. Veritabanına yazmaz.

CANLI BESLEMEYE HAZIR: Ayrıştırıcı bir dosyaya değil, kayıt
LİSTESİNE bağlıdır (`parse_source_a`). Dosya okuma yalnızca
`load_source_payload` içindedir; canlı API bağlandığında o fonksiyon
yerine istek sonucu verilir, geri kalan katmanların hiçbiri değişmez.

Alan adları da tek bir şemaya bağlı değildir. Aynı bilgi hem
snake_case (`dep_time_utc`, `dep_iata`) hem camelCase
(`depScheduledUtc`, `depIata`) şemasıyla okunabilir; hangisi gelirse
gelsin aynı Flight alanlarına düşer. `direction` ve `location` kayıtta
HAZIR geliyorsa doğrudan kullanılır, tekrar hesaplanmaz.
"""

import json
import logging
import re
from datetime import datetime, timezone

from ...queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)
from ...queue.domain.retention_time import canonical_operational_time
from ...queue.domain.schengen import requires_passport_control

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^A-Z0-9]")

# ADIM (Malformed Record Isolation) - bir kaydın alan tiplerinin
# beklenmedikten (ör. `dep_time_utc` string yerine int) doğabilecek,
# TEK BİR bozuk kaydın ayrıştırmasını başarısız kılan ama SİSTEM
# seviyesinde bir arıza OLMAYAN hatalar. `field()`/`clean_text()`
# metin-olmayan değerleri OLDUĞU GİBİ geçirir (uydurma bir dönüşüm
# YAPMAZ) - bu yüzden sonraki `.upper()`/`strptime()` gibi çağrılar
# TypeError/AttributeError fırlatabilir; sözlük erişimleri `field()`
# üzerinden `.get()` ile yapıldığından KeyError normalde beklenmez
# ama gelecekte eklenecek bir yardımcı fonksiyon için güvenlik payı
# olarak listede tutulur. `MemoryError`/`OSError`/config/programlama
# hataları BURADA YAKALANMAZ - bunlar kayıt-seviyesi değil, sistem
# seviyesi arızalardır ve olduğu gibi yukarı taşınmalıdır.
_MALFORMED_RECORD_EXCEPTIONS = (TypeError, ValueError, KeyError, AttributeError)


def _record_context(record: dict) -> str:
    """
    Bozuk kayıt log satırı için tanı bağlamı - ham `record` HİÇBİR
    ZAMAN olduğu gibi loglanmaz (Bölüm: "full raw payload gereksiz
    yere loglanmaz"), sadece teşhis için yeterli birkaç alan. `.get()`
    kullanılır - `record`'un KENDİSİ bozuk/beklenmedik tipte olsa bile
    (ör. alan değerleri int) bu erişim ASLA kendisi bir hataya yol
    AÇMAZ.
    """
    def _safe(*names):
        for name in names:
            value = record.get(name) if isinstance(record, dict) else None
            if value is not None:
                return value
        return None

    return (
        f"flight_iata={_safe('flight_iata', 'flightIata')!r} "
        f"flight_icao={_safe('flight_icao', 'flightIcao')!r} "
        f"dep_iata={_safe('dep_iata', 'depIata')!r} "
        f"arr_iata={_safe('arr_iata', 'arrIata')!r}"
    )

# Kaynaklarda görülen zaman biçimleri. Canlı feed ISO 8601 de
# gönderebilir; hepsi naive UTC datetime'a indirgenir çünkü tüm iç
# hesaplamalar UTC üzerinden yapılır.
_TIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)

# Ham metinde "yok" anlamına gelen gösterimler.
_NULL_TOKENS = {"", "-", "--", "n/a", "na", "null", "none", "unknown"}

# ADIM (Aircraft Match Safety Guard) - Kaynak B kaydının `updated`
# alanı bir ADS-B GÖZLEM zaman damgasıdır (bkz. `_parse_source_b_
# timestamp`), tarife saati DEĞİLDİR - bu yüzden Kaynak A'nın
# `operational_scheduled`'ına yakınlığı sadece BİR KANIT/olasılık
# sinyalidir, ASLA tam eşitlik beklenmez. Ama bu yakınlık sınırsız
# olamaz: aynı flight number GÜNLER sonra farklı bir uçak tipiyle
# uçurulmuş olabilir (filo rotasyonu, mevsimsel değişim, vb.) - gerçek
# veride (`response-delays.json`) TK2025 için TEK aday 16 GÜN eski
# çıktı ve önceki (sınırsız) mantık bunu sessizce kabul ediyordu, bu
# TEHLİKELİ BİR YANLIŞ POZİTİF üretir. 36 saat (aynı/önceki günün
# gerçek uçuşu) konservatif ama günlük frekanslı bir hat için hâlâ
# anlamlı bir kanıt penceresidir; daha eski hiçbir aday güvenilir
# SAYILMAZ ve reddedilir.
AIRCRAFT_MATCH_MAX_AGE_HOURS = 36.0


def normalize_flight_number(value: str | None) -> str | None:
    """
    Uçuş numarasını eşleştirilebilir hale getirir.

    "AC 72"  -> "AC72"
    "ac-72"  -> "AC72"
    None/""  -> None
    """
    if not value:
        return None
    cleaned = _NON_ALNUM.sub("", value.upper())
    return cleaned or None


def clean_text(value):
    """
    Ham alanı temizler. `"-"` gibi "yok" gösterimleri None'a döner,
    böylece sahte bir terminal/kapı/uçak tipi değeri üretilmez.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.lower() in _NULL_TOKENS:
        return None
    return stripped


def field(record: dict, *names):
    """
    Aynı bilginin farklı şemalardaki adlarını sırayla dener.

    Böylece yeni bir besleme geldiğinde ayrıştırıcıyı yeniden yazmak
    yerine buraya bir ad eklemek yeterli olur.
    """
    for name in names:
        value = clean_text(record.get(name))
        if value is not None:
            return value
    return None


def parse_utc(value) -> datetime | None:
    """
    UTC zaman metnini naive datetime'a çevirir.

    Bilinen biçimlerin hiçbiri tutmazsa ISO 8601 denenir ("...Z" veya
    "+03:00" ekli olabilir); saat dilimi bilgisi UTC'ye çevrilip
    düşürülür. Hiçbiri olmazsa None - uydurma zaman üretilmez.
    """
    value = clean_text(value)
    if not value:
        return None

    for time_format in _TIME_FORMATS:
        try:
            return datetime.strptime(value, time_format)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def load_source_payload(path: str) -> list[dict]:
    """
    Kaynak dosyasını okuyup kayıt listesini döndürür.

    Dosya {"response": [...]} sarmalayıcısıyla da, doğrudan liste
    olarak da gelebilir; ikisi de desteklenir.
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict):
        records = payload.get("response", [])
    else:
        records = payload

    return [r for r in records if isinstance(r, dict)]


def _parse_source_b_timestamp(record: dict) -> datetime | None:
    """
    Kaynak B (`flights`, canlı ADS-B) kaydının zaman damgasını okur.

    ÖNEMLİ: Kaynak B'nin GERÇEK response'unda `dep_time_utc`/
    `arr_time_utc` gibi tarife alanları YOKTUR - bu alanlar sadece
    Kaynak A'ya (schedules/delays) özgüdür. `flights` endpoint'i
    9625 kayıtlık gerçek örneklemde TEK zaman sinyali olarak
    `updated` (UNIX epoch, saniye) taşır - "bu pozisyon ne zaman
    görüldü" anlamına gelir, bir tarife saati DEĞİLDİR ama flight
    number eşleşmesini hangi GÜNE ait olduğuna göre süzmek için
    yeterli bir yaklaşık göstergedir.

    `updated` yoksa (veya sayısal değilse) None döner - uydurma zaman
    üretilmez, o kayıt eşleştirmede kullanılmaz.
    """
    raw = field(record, "updated", "updatedAt")
    if raw is None:
        return None
    try:
        epoch_seconds = float(raw)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def build_aircraft_index(source_b_records: list[dict]) -> dict[str, list[dict]]:
    """
    Kaynak B'den {normalize edilmiş uçuş no: [{'icao', 'time', 'dep_iata',
    'arr_iata'}, ...]} indeksi.

    `time`, Kaynak B kaydının GERÇEK zaman alanı olan `updated`
    (UNIX epoch) alanından türetilir - bkz. `_parse_source_b_timestamp`.
    Uçak tipi olmayan veya zamanı çözülemeyen kayıtlar indekse girmez;
    zamanı olmayan bir aday, tarih kontrolü YAPILAMAYACAĞI için
    güvenli bir eşleşme üretemez (bkz. `parse_source_a_record`).

    ADIM (Aircraft Match Safety Guard) - `dep_iata`/`arr_iata` da
    saklanır: sadece flight number eşleşmesi (ör. "TK2025") tek
    başına yeterli DEĞİLDİR - aynı numara farklı bir rotada da
    görülmüş olabilir. Rota bilgisi olmadan `parse_source_a_record`
    güvenli bir rota kontrolü yapamaz.
    """
    index: dict[str, list[dict]] = {}
    skipped_malformed = 0
    for record in source_b_records:
        try:
            icao = (field(record, "aircraft_icao", "aircraftIcao") or "").upper()
            if not icao:
                continue

            time_utc = _parse_source_b_timestamp(record)
            if not time_utc:
                continue

            entry = {
                "icao": icao,
                "time": time_utc,
                "dep_iata": (field(record, "dep_iata", "depIata") or "").upper() or None,
                "arr_iata": (field(record, "arr_iata", "arrIata") or "").upper() or None,
            }

            for name in ("flight_iata", "flightIata", "flight_icao", "flightIcao"):
                key = normalize_flight_number(record.get(name))
                if key:
                    if key not in index:
                        index[key] = []
                    index[key].append(entry)
        except _MALFORMED_RECORD_EXCEPTIONS as exc:
            skipped_malformed += 1
            logger.warning(
                "malformed source-B (live flights) record skipped (%s): %s: %s",
                _record_context(record), type(exc).__name__, exc,
            )
            continue

    if skipped_malformed:
        logger.warning(
            "Source B parse summary: raw=%d indexed_keys=%d skipped_malformed=%d",
            len(source_b_records), len(index), skipped_malformed,
        )
    return index


def build_flight_key(
    airline_iata: str | None,
    flight_number: str | None,
    operational_scheduled_utc: datetime | None,
    airport_iata: str,
    direction: str,
) -> str:
    """
    "{airline_iata}_{flight_number}_{operational_scheduled_date_utc}_{airport_iata}_{direction}"

    MADDE 5: operational_scheduled_utc, yöne göre çağıran tarafından
    seçilir - departure için dep_scheduled_utc, arrival için
    arr_scheduled_utc (bkz. parse_source_a_record). Bu fonksiyon
    yönden habersizdir, sadece kendisine verilen tarihi kullanır.

    estimated/actual zaman değişiklikleri bu anahtarı ETKİLEMEZ -
    sadece scheduled kullanılır; böylece bir uçuş gecikse/erken kalksa
    bile aynı flight_key'e sahip olmaya devam eder ve refresh UPSERT
    yapar (YASAK 4), yeni satır açmaz.

    operational_scheduled_utc None ise (kaynakta scheduled zaman hiç
    yoksa) tarih kısmı "UNKDATE" - bu SESSİZ bir yanlış tarihe düşme
    değil, açıkça işaretli bir controlled fallback'tır.
    """
    airline = (airline_iata or "UNK").upper()
    number = flight_number or "UNK"
    date_part = (
        operational_scheduled_utc.date().isoformat()
        if operational_scheduled_utc else "UNKDATE"
    )
    return f"{airline}_{number}_{date_part}_{airport_iata.upper()}_{direction.lower()}"


def resolve_location(
    dep_iata: str | None,
    arr_iata: str | None,
    country_by_iata: dict[str, str],
) -> str:
    """
    Kalkış ve varış aynı ülkedeyse domestic, değilse international.

    SADECE kayıtta `location` alanı YOKSA çalışır. Alan hazır geldiğinde
    ülke karşılaştırması gereksizdir ve yapılmaz.

    Ülke bilgisi bulunamıyorsa international varsayılır: passport
    yükünü eksik saymak, fazla saymaktan daha risklidir.
    """
    dep_country = country_by_iata.get((dep_iata or "").upper())
    arr_country = country_by_iata.get((arr_iata or "").upper())

    if dep_country and arr_country and dep_country == arr_country:
        return LOCATION_DOMESTIC
    return LOCATION_INTERNATIONAL


def read_direction(record: dict, default: str) -> str:
    """
    Kayıttaki `direction` hazırsa kullanılır, tekrar çıkarılmaz.
    Yoksa beslemenin kendi yönü (arrivals/departures dosyası) geçerlidir.
    """
    value = (field(record, "direction") or "").lower()
    if value in (DIRECTION_ARRIVAL, DIRECTION_DEPARTURE):
        return value
    return default


def read_location(
    record: dict, dep_iata: str | None, arr_iata: str | None,
    country_by_iata: dict[str, str],
) -> str:
    """Kayıttaki `location` hazırsa kullanılır; yoksa türetilir."""
    value = (field(record, "location") or "").lower()
    if value in (LOCATION_DOMESTIC, LOCATION_INTERNATIONAL):
        return value
    return resolve_location(dep_iata, arr_iata, country_by_iata)


def read_flight_number(record: dict) -> str | None:
    """
    Uçuş numarası. Besleme yalnızca tam kodu ("AC72") veriyorsa
    baştaki havayolu harfleri ayrılır.
    """
    number = field(record, "flight_number", "flightNumber", "flightNo")
    if number and not number.strip().isdigit():
        digits = normalize_flight_number(number) or ""
        trimmed = digits.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        return trimmed or digits or None
    return number


def _physical_flight_key(record: dict) -> tuple:
    """
    Aynı fiziksel uçuşun TÜM codeshare kopyalarının PAYLAŞTIĞI (gerçek
    veride doğrulanmış - bkz. `dedupe_codeshares` docstring'i) alanlar:
    kalkış/varış havalimanı + tarifeli kalkış/varış saati. Sadece
    zaman/rota çakışması TEK BAŞINA farklı uçuşları birleştirmeye YETMEZ
    (kullanıcı talebi) - bu anahtar SADECE `dedupe_codeshares()` içinde,
    `cs_flight_iata` sinyaliyle DOĞRULANMIŞ bir grup içinde kullanılır.
    """
    return (
        (field(record, "dep_iata", "depIata") or "").upper(),
        (field(record, "arr_iata", "arrIata") or "").upper(),
        field(record, "dep_time_utc", "depScheduledUtc"),
        field(record, "arr_time_utc", "arrScheduledUtc"),
    )


def dedupe_codeshares(records: list[dict]) -> list[dict]:
    """
    ADIM (Codeshare Duplicate Physical Flights) - AirLabs contract'ı
    doğrulanmış: `cs_flight_iata` doluysa bu kayıt, o alanın gösterdiği
    flight_iata'ya sahip GERÇEK/operating fiziksel uçuşun bir pazarlama
    (marketing) kopyasıdır - aynı dep/arr havalimanı VE aynı tarifeli
    dep/arr saatini BİREBİR paylaşır (gerçek ZRH verisinde doğrulandı:
    LX972 operating + SQ2932/CX6615/AC6766/AZ3844 aynı fiziksel uçuşun
    4 pazarlama kodu, ikisi de dep/arr/saat alanları AYNI).

    Kural (kullanıcı talebi, Bölüm 2/3):
      1) `cs_flight_iata` BOŞ olan kayıt (operating) HER ZAMAN korunur.
      2) `cs_flight_iata` DOLU bir kayıt, EĞER aynı batch'te GERÇEKTEN
         operating bir eş (aynı fiziksel anahtar + `cs_flight_iata` boş)
         varsa ATLANIR - fiziksel uçuş zaten operating kayıtla temsil
         ediliyor.
      3) Eğer bir fiziksel anahtar grubunda operating eş YOKSA (source
         "only-codeshare" - operating kaydı hiç gelmemiş), fiziksel
         uçuş TAMAMEN KAYBOLMASIN diye grup içinden TEK bir canonical
         kayıt seçilir (deterministic: normalize edilmiş flight_iata'sı
         alfabetik en küçük olan).
      4) Bir fiziksel anahtar grubunda `cs_flight_iata` dolu HİÇ kayıt
         yoksa (hepsi "operating" görünüyor) - bu SADECE zaman/rota
         çakışmasıdır, codeshare KANITI yoktur - HİÇBİRİ atlanmaz
         (güvenli taraf: "sadece zaman/rota eşleşmesiyle dedup yapma").

    Bu, canonical dedup'ın TEK uygulandığı yerdir (`parse_source_a()`
    tarafından, per-record ayrıştırmadan ÖNCE çağrılır) - başka hiçbir
    katmanda TEKRAR filtrelenmez.
    """
    groups: dict[tuple, list[dict]] = {}
    for record in records:
        groups.setdefault(_physical_flight_key(record), []).append(record)

    result: list[dict] = []
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        operating = [
            r for r in group
            if not field(r, "cs_flight_iata", "csFlightIata")
        ]
        codeshares = [
            r for r in group
            if field(r, "cs_flight_iata", "csFlightIata")
        ]

        if not codeshares:
            # Codeshare KANITI yok - salt zaman/rota çakışması, hiçbiri atlanmaz.
            result.extend(group)
        elif operating:
            # Normal durum: operating kayıt(lar) korunur, TÜM codeshare'ler atılır.
            result.extend(operating)
        else:
            # "Only-codeshare" fallback - operating kayıt hiç yok, fiziksel
            # uçuş kaybolmasın diye TEK canonical kayıt seçilir.
            canonical = min(
                codeshares,
                key=lambda r: normalize_flight_number(
                    field(r, "flight_iata", "flightIata")
                ) or "",
            )
            result.append(canonical)

    return result


def parse_source_a_record(
    record: dict,
    direction: str,
    country_by_iata: dict[str, str],
    aircraft_index: dict[str, str] | None = None,
    min_operational_time: datetime | None = None,
) -> dict | None:
    """
    Kaynak A kaydını Flight alanlarına eşler ve Kaynak B ile
    zenginleştirir.

    Alan adları iki şemadan da okunur (bkz. `field`), böylece canlı
    beslemeye geçiş bu fonksiyonun dışında hiçbir katmanı etkilemez.

    aircraft_icao önceliği:
      1) Kaynak A'nın kendi alanı (doluysa)
      2) Kaynak B eşleşmesi
      3) None - uydurma değer ÜRETİLMEZ

    min_operational_time : ADIM (Re-Ingest Loop Prevention) - Bölüm 15.
        Verilirse, kaydın canonical operasyonel zamanı (`domain/
        retention_time.py:canonical_operational_time()` - flight_key ile
        AYNI departure/arrival seçimi) bu değerden KESİN OLARAK küçükse
        (`<`, sınır dahil DEĞİL - Bölüm 7'deki `timestamp == cutoff ->
        retained` kuralıyla TUTARLI) kayıt HİÇ üretilmez (None döner) -
        retention tarafından silinmiş eski bir flight'ın upstream hâlâ
        döndürdüğü için sonsuza kadar yeniden INSERT edilmesi
        (re-ingest loop) böylece önlenir. Canonical zaman bilinmiyorsa
        (None) kayıt YİNE DE üretilir - bilinmeyen bir zaman "eski"
        sayılıp SESSİZCE atılmaz. Verilmezse (None, varsayılan) HİÇ
        filtre uygulanmaz - eski davranış birebir korunur.
    """
    dep_iata = (field(record, "dep_iata", "depIata") or "").upper() or None
    arr_iata = (field(record, "arr_iata", "arrIata") or "").upper() or None

    direction = read_direction(record, direction)
    airport_iata = dep_iata if direction == DIRECTION_DEPARTURE else arr_iata
    if not airport_iata:
        return None

    dep_scheduled = parse_utc(
        field(record, "dep_time_utc", "depScheduledUtc")
    )
    arr_scheduled = parse_utc(
        field(record, "arr_time_utc", "arrScheduledUtc")
    )
    # MADDE 5: operasyonel tarih yöne göre seçilir - departure için
    # dep_scheduled_utc, arrival için arr_scheduled_utc. Arrival
    # kaydında dep_scheduled boş olsa bile (arr_scheduled doluysa)
    # key artık UNKDATE'e düşmez.
    operational_scheduled = canonical_operational_time(
        direction, dep_scheduled, arr_scheduled
    )

    if (
        min_operational_time is not None
        and operational_scheduled is not None
        and operational_scheduled < min_operational_time
    ):
        return None
    airline_iata = (
        field(record, "airline_iata", "airlineIata", "airline") or ""
    ).upper() or None
    flight_number = read_flight_number(record)

    flight_code = field(record, "flight_iata", "flightIata", "flightNo")
    flight_icao = field(record, "flight_icao", "flightIcao")

    # Kayıtta uçuşu tekilleştirecek HİÇBİR alan yoksa (flight_number,
    # flight_iata, flight_icao, airline_iata hepsi boş) build_flight_key
    # sabit bir "UNK_UNK_<tarih>_<havalimanı>_<yön>" anahtarı üretir - aynı
    # gün/havalimanı/yöndeki FARKLI fiziksel uçuşlar bu anahtarda çakışıp
    # birbirinin UPSERT'iyle SESSİZCE ezilir. Böyle bir kayıt hiçbir
    # tekilleştirici alan taşımıyorsa güvenle işlenemez; atlanır.
    if not (flight_number or flight_code or flight_icao or airline_iata):
        return None

    own_icao = (
        field(record, "aircraft_icao", "aircraftIcao") or ""
    ).upper() or None
    matched_icao = None
    if own_icao is None and aircraft_index and operational_scheduled:
        # ADIM (Aircraft Match Safety Guard) - flight number eşleşmesi
        # TEK BAŞINA yeterli değildir: aynı numara farklı bir rotada
        # görülmüş olabilir (B) rota da UYMALI), VE Kaynak B'nin
        # `updated` gözlem zamanı `operational_scheduled`'dan makul
        # (`AIRCRAFT_MATCH_MAX_AGE_HOURS`) bir pencere içinde olmalı -
        # bkz. o sabitin docstring'i (16 gün eski TEK aday gerçek
        # veride görüldü, bu ASLA güvenilir bir eşleşme değildir).
        # `updated` bir TARİFE saati DEĞİL bir ADS-B gözlem zamanı
        # olduğu için tam eşitlik ASLA aranmaz - sadece yakınlık kanıtı.
        raw_candidates = []
        for candidate in (flight_code, flight_icao):
            key = normalize_flight_number(candidate)
            if key and key in aircraft_index:
                raw_candidates.extend(aircraft_index[key])

        eligible = []
        for candidate_match in raw_candidates:
            # B) + C) rota EŞLEŞMELİ - taraflardan biri bilinmiyorsa
            # (None) rota doğrulanamaz, güvenli tarafta kalıp reddedilir.
            if (
                candidate_match["dep_iata"] is None
                or dep_iata is None
                or candidate_match["dep_iata"] != dep_iata
            ):
                continue
            if (
                candidate_match["arr_iata"] is None
                or arr_iata is None
                or candidate_match["arr_iata"] != arr_iata
            ):
                continue

            age_hours = abs(
                (candidate_match["time"] - operational_scheduled).total_seconds()
            ) / 3600.0
            if age_hours > AIRCRAFT_MATCH_MAX_AGE_HOURS:
                continue

            eligible.append((age_hours, candidate_match["icao"]))

        if eligible:
            best_age = min(age for age, _ in eligible)
            best_icaos = {icao for age, icao in eligible if age == best_age}
            # G) en yakın adaylar birbiriyle ÇELİŞEN farklı ICAO'lar
            # taşıyorsa TAHMİN YÜRÜTÜLMEZ - eşleşme None kalır.
            if len(best_icaos) == 1:
                matched_icao = next(iter(best_icaos))

    aircraft_icao = own_icao or matched_icao

    # ADIM (Schengen-Aware Passport Routing) - `Airport.country_code`
    # çözümünden (AYNI `country_by_iata` haritası, `resolve_location()`'ın
    # kullandığı KAYNAK) - flight number/airline üzerinden TAHMİN
    # YAPILMAZ. `location` (traffic type) BURADAN ETKİLENMEZ/DEĞİŞMEZ -
    # bu SADECE ayrı, ek bir sınır-kontrolü sinyalidir (bkz. `domain/
    # schengen.py` modül docstring'i).
    dep_country = country_by_iata.get((dep_iata or "").upper())
    arr_country = country_by_iata.get((arr_iata or "").upper())
    requires_passport = requires_passport_control(dep_country, arr_country)

    return {
        "flight_key": build_flight_key(
            airline_iata, flight_number, operational_scheduled, airport_iata, direction
        ),
        "airport_iata": airport_iata,
        "direction": direction,
        "location": read_location(
            record, dep_iata, arr_iata, country_by_iata
        ),
        "requires_passport": requires_passport,
        "airline_iata": airline_iata,
        "flight_number": flight_number,
        "flight_iata": normalize_flight_number(flight_code),
        "aircraft_icao": aircraft_icao,
        "aircraft_match_found": aircraft_icao is not None,
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "dep_scheduled_utc": dep_scheduled,
        "dep_estimated_utc": parse_utc(
            field(record, "dep_estimated_utc", "depEstimatedUtc")
        ),
        "dep_actual_utc": parse_utc(
            field(record, "dep_actual_utc", "depActualUtc")
        ),
        "arr_scheduled_utc": arr_scheduled,
        "arr_estimated_utc": parse_utc(
            field(record, "arr_estimated_utc", "arrEstimatedUtc")
        ),
        "arr_actual_utc": parse_utc(
            field(record, "arr_actual_utc", "arrActualUtc")
        ),
        "dep_terminal": field(record, "dep_terminal", "depTerminal"),
        "dep_gate": field(record, "dep_gate", "depGate"),
        "arr_terminal": field(record, "arr_terminal", "arrTerminal"),
        "arr_gate": field(record, "arr_gate", "arrGate"),
        "status": (field(record, "status") or "unknown").lower(),
    }


def parse_source_a(
    records: list[dict],
    direction: str,
    country_by_iata: dict[str, str],
    aircraft_index: dict[str, str] | None = None,
    min_operational_time: datetime | None = None,
) -> list[dict]:
    """
    Kaynak A kayıt listesini Flight sözlüklerine çevirir. `min_operational_time`:
    bkz. `parse_source_a_record()`.

    ADIM (Malformed Record Isolation) - TEK bir kaydın alan tipi/biçimi
    beklenmedikse (`_MALFORMED_RECORD_EXCEPTIONS`) o kayıt loglanıp
    ATLANIR, TÜM batch/refresh cycle'ı İPTAL EDİLMEZ - önceki davranışta
    `parse_source_a_record()`'ın fırlattığı herhangi bir istisna bu
    döngüde YAKALANMIYORDU, bu yüzden bir havalimanının tek bozuk kaydı
    `load_flight_rows()` -> `pipeline.run()` zincirinin TAMAMINI
    (TÜM havalimanları, HER İKİ yön) başarısız kılabiliyordu (bkz.
    pre-production audit raporu). Sistem seviyesi hatalar (DB, bellek,
    config, programlama hatası) BU except'e GİRMEZ, olduğu gibi yukarı
    taşınmaya devam eder - geniş bir `except Exception` KASITLI OLARAK
    kullanılmadı.
    """
    # ADIM (Codeshare Duplicate Physical Flights) - per-record ayrıştırmadan
    # ÖNCE, TEK canonical noktada uygulanır (bkz. `dedupe_codeshares()`
    # docstring'i) - aynı fiziksel uçuş birden fazla kez passenger demand
    # ÜRETMESİN diye pazarlama/codeshare kopyaları burada elenir.
    raw_count = len(records)
    records = dedupe_codeshares(records)
    codeshares_skipped = raw_count - len(records)
    if codeshares_skipped:
        logger.info(
            "Source A codeshare dedup (direction=%s): raw=%d physical=%d skipped=%d",
            direction, raw_count, len(records), codeshares_skipped,
        )

    parsed = []
    skipped_malformed = 0
    for record in records:
        try:
            row = parse_source_a_record(
                record, direction, country_by_iata, aircraft_index,
                min_operational_time=min_operational_time,
            )
        except _MALFORMED_RECORD_EXCEPTIONS as exc:
            skipped_malformed += 1
            logger.warning(
                "malformed source-A record skipped (direction=%s, %s): %s: %s",
                direction, _record_context(record), type(exc).__name__, exc,
            )
            continue
        if row is not None:
            parsed.append(row)

    if skipped_malformed:
        logger.warning(
            "Source A parse summary (direction=%s): raw=%d parsed=%d skipped_malformed=%d",
            direction, len(records), len(parsed), skipped_malformed,
        )
    return parsed


def aircraft_match_rate(rows: list[dict]) -> float:
    """
    Uçak tipi çözülebilen uçuşların oranı (AŞAMA 7 confidence girdisi).
    Hiç uçuş yoksa 0.0 - sahte %100 üretilmez.
    """
    if not rows:
        return 0.0
    matched = sum(1 for r in rows if r.get("aircraft_match_found"))
    return matched / len(rows)


__all__ = [
    "DIRECTION_ARRIVAL",
    "DIRECTION_DEPARTURE",
    "aircraft_match_rate",
    "build_aircraft_index",
    "build_flight_key",
    "clean_text",
    "field",
    "load_source_payload",
    "normalize_flight_number",
    "parse_source_a",
    "parse_source_a_record",
    "parse_utc",
    "read_direction",
    "read_flight_number",
    "read_location",
    "resolve_location",
]
