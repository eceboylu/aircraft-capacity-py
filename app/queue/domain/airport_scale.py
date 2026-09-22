"""
ADIM (Airport-Scale Queue Capacity) - havalimanı ölçek sınıflandırması
(large/medium/small) ve ölçeğe göre queue kaynak (server/lane) eşlemesi
için TEK merkezi kaynak.

SAF KATMAN: veritabanına/dosyaya kalıcı yazmaz - `domain/operational_day.py`
ile AYNI desende, sadece OKUR (txt dosyası) ve saf fonksiyonlarla
çözümler. Kalıcı import (`Airport.scale`'i DB'ye yazmak)
`ingestion/airports_import.py`'nin sorumluluğudur - bu modül SADECE
parse/resolve mantığını taşır.

Kaynak dosyalar (gerçek, `data/` altında):
  - data/buyuk_olcekli_havaalanlari.txt   (large)
  - data/orta_olcekli_havaalanlari.txt    (medium)
  - data/kucuk_olcekli_havaalanlari.txt   (small)

Format (gerçek dosyalarda doğrulandı):
    BÜYÜK ÖLÇEKLİ HAVAALANLARI
    Toplam: 1.138
    Biçim: IATA / ICAO - Havaalanı adı

    GUM / PGUM - A.B. Won Pat International Airport
    ...

`---` ICAO alanı "bilinmiyor" anlamına gelir - geçerli bir ICAO kodu
olarak SAYILMAZ (IATA eşleşmesi hâlâ çalışır, ICAO fallback için bu
havalimanı adaylardan düşer).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SCALE_LARGE = "large"
SCALE_MEDIUM = "medium"
SCALE_SMALL = "small"

SCALES = (SCALE_LARGE, SCALE_MEDIUM, SCALE_SMALL)

# Tek doğruluk kaynağı - genel-proje.md'nin YENİ RESOURCE CONTRACT'ı.
# Passport service time = 1.5 dk/passenger/server, security = 1 dk/
# passenger/lane (bu modülde SAKLANMAZ - core/scoring.py'nin config'ten
# okuduğu, ZATEN var olan alanlardır; burada SADECE server/lane SAYILARI
# tutulur).
#
# ADIM (Generic Scale Resource Update) - gerçek dünya kanıtına dayanarak
# (bkz. rapor: IST 68 departure passport gişesi, SIN ~130 otomatik
# immigration lane) güncellendi. Alan adları/mapping DEĞİŞMEDİ - SADECE
# "large/medium/small" tier'larının SAYISAL değerleri:
#   "domestic_security_lanes"    = Domestic/Landside Security
#   "international_security_lanes" = International/Airside/Transfer Security
# (mevcut terminoloji AYNEN korundu, yeni paralel bir alan/isim
# İCAT EDİLMEDİ). Havalimanı-özel gerçek değerler (ör. IST/SIN'in
# gerçek gişe sayıları) bu GENERIC tier sabitlerine YAZILMADI -
# `AirportOperationalConfig` üzerinden airport-specific override
# mekanizması (config.py öncelik zinciri, bu tablonun ÜSTÜNDE) buna
# ayrılmıştır; bu sözlük SADECE "hiç override'ı olmayan" havalimanları
# için generic varsayımdır.
SCALE_RESOURCES: dict[str, dict[str, int]] = {
    # ADIM (Dynamic LARGE Passport Staffing) - `departure_passport_
    # servers`/`arrival_passport_servers` LARGE için artık "sabit
    # server sayısı" DEĞİL, dinamik staffing'in ASLA ALTINA
    # DÜŞMEYECEĞİ default/taban değeridir (bkz. config.py/engine.py).
    # `_max` anahtarı SADECE LARGE'da var - bir ölçek sözlüğünde bu
    # anahtar YOKSA (MEDIUM/SMALL/UNKNOWN) o ölçek SABİT/statik kalır,
    # dynamic staffing hiç devreye girmez (bkz. config.py
    # `_resolve_passport_server_counts`).
    #
    # ADIM (VERY_LARGE Tier Removed) - önceki turda eklenen ayrı
    # VERY_LARGE/HUB tier'ı (IST/AMS'i LARGE'dan ayıran) TAMAMEN
    # KALDIRILDI - kullanıcı TÜM LARGE havalimanlarının (IST/AMS/SAW
    # dahil) AYNI tek contract'ı paylaşmasını istedi, airport-özel
    # hardcode YOK. Bu yüzden LARGE'ın `domestic_security_lanes`/
    # `international_security_lanes` değeri artık eski VERY_LARGE
    # tier'ının sayısı (20) - security lane-count contract'ı GERİ
    # DÜŞÜRÜLMEDİ, SADECE tek bir tier'a birleştirildi.
    #
    # ADIM (Security Capacity Contract v4) - security artık `security_
    # service_time_minutes` üzerinden DOLAYLI değil, `SECURITY_
    # PASSENGERS_PER_HOUR_PER_LANE` (constants.py, tek source-of-truth,
    # =150) İLE DOĞRUDAN "lane_count x 150 pax/saat" olarak okunur -
    # buradaki sayılar SADECE lane_count, throughput/lane HER ölçek
    # için AYNI sabittir. v3'te domestic/international BİLEREK eşit
    # (symmetric) tutulmuştu - kullanıcı bu ADIM'da AÇIKÇA farklı lane
    # sayıları verdi (LARGE: domestic=14/international=30, MEDIUM:
    # 6/8, SMALL: 2/2) - domestic/international HÂLÂ AYRI, bağımsız
    # queue pool (Bölüm 4 - ayrı incoming/backlog/served/utilization/
    # wait), SADECE artık aynı lane SAYISINI PAYLAŞMIYORLAR. Passport
    # tarafı bu ADIM'da DOKUNULMADI.
    SCALE_LARGE: {
        "departure_passport_servers": 30,
        "departure_passport_servers_max": 70,
        "arrival_passport_servers": 45,
        "arrival_passport_servers_max": 75,
        "domestic_security_lanes": 14,
        "international_security_lanes": 30,
    },
    SCALE_MEDIUM: {
        "departure_passport_servers": 10,
        "arrival_passport_servers": 15,
        "domestic_security_lanes": 6,
        "international_security_lanes": 8,
    },
    SCALE_SMALL: {
        "departure_passport_servers": 3,
        "arrival_passport_servers": 4,
        "domestic_security_lanes": 2,
        "international_security_lanes": 2,
    },
}

# "Biçim: IATA / ICAO - Havaalanı adı" başlık satırı da AYNI regex'e
# uyar (IATA/ICAO kelimeleri 2-4 harf) - bu yüzden literal placeholder
# metni AÇIKÇA dışlanır (aşağıda _is_header_placeholder).
_LINE_RE = re.compile(r"^([A-Za-z0-9]{2,4})\s*/\s*([A-Za-z0-9-]{2,4})\s*-\s*(.+)$")
_NO_ICAO_TOKENS = {"", "-", "--", "---"}


def _is_header_placeholder(iata: str, icao: str) -> bool:
    """"Biçim: IATA / ICAO - ..." başlık satırını airport kaydı SAYMAZ."""
    return iata.upper() == "IATA" or icao.upper() == "ICAO"


@dataclass(frozen=True)
class ScaleCodeSet:
    """Bir ölçek dosyasından ayrıştırılmış, aranabilir IATA/ICAO kod kümeleri."""

    iata_codes: frozenset[str] = field(default_factory=frozenset)
    icao_codes: frozenset[str] = field(default_factory=frozenset)


def parse_scale_list(path: str) -> ScaleCodeSet:
    """
    Bir ölçek txt dosyasını ayrıştırır.

    - Whitespace normalize edilir (`.strip()` + regex'in kendi `\\s*`'i).
    - Header/Toplam/Biçim satırları (regex'e uymayan VEYA literal
      "IATA"/"ICAO" placeholder'ı taşıyan) airport SAYILMAZ.
    - `---` (veya boş) ICAO alanı geçerli bir ICAO kodu SAYILMAZ - IATA
      eşleşmesi hâlâ çalışır, sadece ICAO-fallback kümesine girmez.
    - Aynı dosya içinde tekrar eden IATA kodu tek kez tutulur (set) -
      duplicate SAYIM burada yapılmaz, ayrı bir denetim fonksiyonuna
      (bkz. `find_duplicate_iata`) bırakılır - bu fonksiyon SESSİZCE
      dedupe eder, hatayı GİZLEMEZ (çağıran taraf, testte, duplicate'i
      AYRICA denetler).
    """
    iata_codes: set[str] = set()
    icao_codes: set[str] = set()

    with open(path, encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            match = _LINE_RE.match(line)
            if not match:
                continue
            iata_raw, icao_raw, _name = match.groups()
            iata = iata_raw.strip().upper()
            icao = icao_raw.strip().upper()
            if _is_header_placeholder(iata, icao):
                continue
            if iata:
                iata_codes.add(iata)
            if icao and icao not in _NO_ICAO_TOKENS:
                icao_codes.add(icao)

    return ScaleCodeSet(iata_codes=frozenset(iata_codes), icao_codes=frozenset(icao_codes))


def find_duplicate_iata(path: str) -> list[str]:
    """Aynı dosya İÇİNDE birden fazla kez görünen IATA kodları (varsa)."""
    counts: dict[str, int] = {}
    with open(path, encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            match = _LINE_RE.match(line)
            if not match:
                continue
            iata = match.group(1).strip().upper()
            icao = match.group(2).strip().upper()
            if _is_header_placeholder(iata, icao) or not iata:
                continue
            counts[iata] = counts.get(iata, 0) + 1
    return sorted(code for code, n in counts.items() if n > 1)


def find_cross_scale_conflicts(
    large: ScaleCodeSet, medium: ScaleCodeSet, small: ScaleCodeSet,
) -> dict[str, dict[str, set[str]]]:
    """
    Aynı IATA/ICAO kodunun BİRDEN FAZLA ölçek dosyasında görünüp
    görünmediğini denetler. Boş dönerse çakışma YOK.

    Dönen şekil: {"iata": {"large&medium": {...}, ...}, "icao": {...}}
    - SADECE gerçekten kesişen kod kümeleri raporlanır (boş kesişim
      anahtar olarak bile YER ALMAZ).
    """
    scales = {SCALE_LARGE: large, SCALE_MEDIUM: medium, SCALE_SMALL: small}
    conflicts: dict[str, dict[str, set[str]]] = {"iata": {}, "icao": {}}

    names = list(scales)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            iata_overlap = scales[a].iata_codes & scales[b].iata_codes
            if iata_overlap:
                conflicts["iata"][f"{a}&{b}"] = iata_overlap
            icao_overlap = scales[a].icao_codes & scales[b].icao_codes
            if icao_overlap:
                conflicts["icao"][f"{a}&{b}"] = icao_overlap

    return conflicts


def resolve_airport_scale(
    iata_code: str | None,
    icao_code: str | None,
    large: ScaleCodeSet,
    medium: ScaleCodeSet,
    small: ScaleCodeSet,
) -> str | None:
    """
    Bir havalimanının ölçeğini çözer.

    Öncelik: ÖNCE IATA exact match (3 ölçek arasında), IATA hiçbirinde
    bulunamazsa ICAO exact match. İkisi de bulunamazsa None (unknown -
    UYDURMA bir varsayılan ÜRETİLMEZ, çağıran taraf - config.py - bunu
    açıkça ele almalı).

    Airport adı üzerinden fuzzy match YOK (görev talimatı) - sadece
    exact kod eşleşmesi.
    """
    iata = (iata_code or "").strip().upper()
    icao = (icao_code or "").strip().upper()
    scales = {SCALE_LARGE: large, SCALE_MEDIUM: medium, SCALE_SMALL: small}

    if iata:
        for scale_name in SCALES:
            if iata in scales[scale_name].iata_codes:
                return scale_name

    if icao:
        for scale_name in SCALES:
            if icao in scales[scale_name].icao_codes:
                return scale_name

    return None


def resource_view_for_scale(scale: str | None) -> dict[str, int] | None:
    """Ölçeğin kaynak eşlemesi (server/lane sayıları). Bilinmeyen/None ölçek için None."""
    if scale is None:
        return None
    return SCALE_RESOURCES.get(scale)
