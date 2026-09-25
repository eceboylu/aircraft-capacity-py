"""
ADIM (4-Tier Airport Scale) - havalimanı ölçek sınıflandırması
(mega/large/medium/small) ve ölçeğe göre queue kaynak (server/lane)
eşlemesi için TEK merkezi kaynak.

SAF KATMAN: veritabanına/dosyaya kalıcı yazmaz - `domain/operational_day.py`
ile AYNI desende, sadece OKUR (txt dosyası) ve saf fonksiyonlarla
çözümler. Kalıcı import (`Airport.scale`'i DB'ye yazmak)
`ingestion/airports_import.py`'nin sorumluluğudur - bu modül SADECE
parse/resolve mantığını taşır.

Kaynak dosyalar (gerçek, `data/` altında):
  - data/mega_havaalanlari.txt            (mega, 40M+ yolcu/yıl)
  - data/buyuk_olcekli_havaalanlari.txt   (large, 5-40M yolcu/yıl)
  - data/orta_olcekli_havaalanlari.txt    (medium, 1-5M yolcu/yıl)
  - data/kucuk_olcekli_havaalanlari.txt   (small, 1M altı yolcu/yıl)

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

SCALE_MEGA = "mega"
SCALE_LARGE = "large"
SCALE_MEDIUM = "medium"
SCALE_SMALL = "small"

# Precedence sırası: bir kod BİRDEN FAZLA listede görünmüyorsa (bkz.
# `find_cross_scale_conflicts` - normal koşulda olmamalı) bu sıra
# `resolve_airport_scale()`'in hangi ölçeği ÖNCE denediğini belirler.
SCALES = (SCALE_MEGA, SCALE_LARGE, SCALE_MEDIUM, SCALE_SMALL)

# Tek doğruluk kaynağı - kullanıcının AÇIKÇA verdiği 4-tier resource
# contract'ı (bkz. görev.md - "4-Tier Airport Scale" + "Dynamic MEGA
# Passport Staffing" task'ları). Passport service time / security
# throughput-per-lane formülleri BU MODÜLDE SAKLANMAZ (core/scoring.py'nin
# config'ten okuduğu, ZATEN var olan alanlardır) - burada SADECE server/
# lane SAYILARI tutulur:
#   "domestic_security_lanes"      = Domestic/Landside Security
#   "international_security_lanes" = International/Airside/Transfer Security
#   "departure_passport_servers"   = Departure (international) Manual Passport
#   "arrival_passport_servers"     = Arrival (international) Manual Passport
#
# ADIM (Dynamic MEGA Passport Staffing) - `_max` anahtarı (`departure_
# passport_servers_max`/`arrival_passport_servers_max`) SADECE MEGA'da
# VAR - bir tier sözlüğünde bu anahtarın VARLIĞI, `config.py:
# _build_config_view()`'ın `departure_max is not None` kontrolü
# üzerinden o tier'ın dynamic staffing'e AÇIK olup olmadığını belirler
# (bkz. o fonksiyonun docstring'i - kontrol KASITLI olarak `scale ==
# "mega"` gibi sabit bir tier adına DEĞİL, bu anahtarın varlığına bağlı,
# scale-agnostik). LARGE/MEDIUM/SMALL HİÇBİR `_max` anahtarı TAŞIMAZ -
# üçü de TAMAMEN STATIC kalır, backlog ne olursa olsun `departure_
# passport_servers`/`arrival_passport_servers` sabit sayısının ÜZERİNE
# HİÇ ÇIKMAZLAR. Security lane sayıları (`domestic_security_lanes`/
# `international_security_lanes`) HİÇBİR tier için dynamic DEĞİLDİR -
# demand'e göre büyüyüp küçülmezler, bu ADIM'ın kapsamı SADECE passport.
#
# ESKİ DURUM (önceki bir turda YANLIŞLIKLA kaldırıldı, bu ADIM'DA
# DÜZELTİLDİ): dynamic staffing bir ara LARGE'a bağlıydı - kullanıcı
# BUNU İSTEMEDİ, dynamic'in MEGA'ya taşınmasını istedi. Mekanizmanın
# KENDİSİ (`DynamicStaffingParams`/`engine.py:simulate_fifo_queue_
# dynamic()`) HİÇ SİLİNMEDİ - hangi tier'ın onu tetiklediği, SADECE bu
# sözlükteki `_max` anahtarının HANGİ tier'da olduğuyla değişir.
#
# Havalimanı-özel gerçek değerler (ör. IST/SIN'in gerçek gişe sayıları)
# bu GENERIC tier sabitlerine YAZILMAZ - `AirportOperationalConfig`
# üzerinden airport-specific override mekanizması (config.py öncelik
# zinciri, bu tablonun ÜSTÜNDE) buna ayrılmıştır; bu sözlük SADECE "hiç
# override'ı olmayan" havalimanları için generic varsayımdır.
SCALE_RESOURCES: dict[str, dict[str, int]] = {
    SCALE_MEGA: {
        "departure_passport_servers": 30,
        "departure_passport_servers_max": 45,
        "arrival_passport_servers": 35,
        "arrival_passport_servers_max": 45,
        "domestic_security_lanes": 30,
        "international_security_lanes": 20,
    },
    SCALE_LARGE: {
        "departure_passport_servers": 10,
        "arrival_passport_servers": 12,
        "domestic_security_lanes": 8,
        "international_security_lanes": 6,
    },
    SCALE_MEDIUM: {
        "departure_passport_servers": 4,
        "arrival_passport_servers": 4,
        "domestic_security_lanes": 3,
        "international_security_lanes": 2,
    },
    SCALE_SMALL: {
        "departure_passport_servers": 2,
        "arrival_passport_servers": 2,
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
    mega: ScaleCodeSet, large: ScaleCodeSet, medium: ScaleCodeSet, small: ScaleCodeSet,
) -> dict[str, dict[str, set[str]]]:
    """
    Aynı IATA/ICAO kodunun BİRDEN FAZLA ölçek dosyasında görünüp
    görünmediğini denetler. Boş dönerse çakışma YOK.

    Dönen şekil: {"iata": {"mega&large": {...}, ...}, "icao": {...}}
    - SADECE gerçekten kesişen kod kümeleri raporlanır (boş kesişim
      anahtar olarak bile YER ALMAZ). 4 tier'ın (mega/large/medium/
      small) TÜM ikili kombinasyonları denetlenir.
    """
    scales = {
        SCALE_MEGA: mega, SCALE_LARGE: large,
        SCALE_MEDIUM: medium, SCALE_SMALL: small,
    }
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
    mega: ScaleCodeSet,
    large: ScaleCodeSet,
    medium: ScaleCodeSet,
    small: ScaleCodeSet,
) -> str | None:
    """
    Bir havalimanının ölçeğini çözer.

    Öncelik: ÖNCE IATA exact match (4 ölçek arasında, `SCALES` sırasıyla
    - mega -> large -> medium -> small), IATA hiçbirinde bulunamazsa
    ICAO exact match (AYNI sıra). İkisi de bulunamazsa None (unknown -
    UYDURMA bir varsayılan ÜRETİLMEZ, çağıran taraf - config.py - bunu
    açıkça ele almalı). Normal koşulda (bkz. `find_cross_scale_conflicts`)
    bir kod SADECE TEK bir ölçek kümesinde bulunur - bu precedence
    sırası sadece çakışma DENETLENMEDEN çağrılırsa hangi ölçeğin
    kazanacağını belirler, çakışma varsa çağıran taraf (`ingestion/
    airports_import.py`) zaten `scale=None` ile ezer.

    Airport adı üzerinden fuzzy match YOK (görev talimatı) - sadece
    exact kod eşleşmesi.
    """
    iata = (iata_code or "").strip().upper()
    icao = (icao_code or "").strip().upper()
    scales = {
        SCALE_MEGA: mega, SCALE_LARGE: large,
        SCALE_MEDIUM: medium, SCALE_SMALL: small,
    }

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
