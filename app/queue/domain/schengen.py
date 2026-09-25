"""
Schengen üyeliği ve pasaport kontrolü gerekliliği - SAF domain katmanı.

ADIM (Schengen-Aware Passport Routing) - kullanıcı doğrulanmış bulgu:
mevcut `resolve_location()` (bkz. `ingestion/sources.py`) SADECE
"aynı ülke -> domestic, farklı ülke -> international" ayrımı yapıyordu
ve bu ayrım TEK BAŞINA passport routing için kullanılıyordu. Bu YANLIŞ:
Schengen alanı İÇİ uçuşlar (ör. ZRH->FRA) GERÇEKTEN international'dır
(gümrük/traffic type anlamında) ama sınırda pasaport kontrolü YOKTUR.

Bu modül İKİ AYRI kavramı kasıtlı olarak AYIRIR:
  - TRAFFIC TYPE (domestic/international) - `resolve_location()`'ın
    ÜRETTİĞİ `Flight.location`, DEĞİŞMEDİ.
  - BORDER CONTROL REQUIREMENT (`requires_passport_control()`) - BU
    modülün ürettiği YENİ, bağımsız sinyal.

Schengen kararı SADECE `Airport.country_code` çözümünden (aynı
`country_by_iata` haritası - `resolve_location()`'ın kullandığı KAYNAK)
gelir; flight number/airline üzerinden TAHMİN YAPILMAZ.
"""

# ISO 3166-1 alpha-2 - Schengen bölgesi üyeleri (fiili iç sınır kontrolü
# kaldırılmış alan). AB üyesi olmayan ama Schengen'e dahil ülkeler (CH,
# NO, IS, LI) DAHİL edilir; AB üyesi olup Schengen'e dahil OLMAYAN
# ülkeler (IE, CY) BİLİNÇLİ OLARAK HARİÇ tutulur.
SCHENGEN_COUNTRY_CODES = frozenset({
    "AT", "BE", "BG", "HR", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE",
    "IS", "LI", "NO", "CH",
})


def is_schengen_country(country_code: str | None) -> bool:
    """Verilen ISO alpha-2 ülke kodu Schengen bölgesi üyesi mi."""
    if not country_code:
        return False
    return country_code.strip().upper() in SCHENGEN_COUNTRY_CODES


def requires_passport_control(
    dep_country: str | None, arr_country: str | None
) -> bool:
    """
    Minimum-safe kural (kullanıcı talebi, Bölüm 8):

      - `dep_country`/`arr_country` bilinmiyorsa (None) -> GEREKİR
        (bilinmeyeni "gerekmiyor" saymak passport yükünü EKSİK sayar -
        `resolve_location()`'ın "ülke bilinmiyorsa international
        varsayılır" ilkesiyle AYNI risk yönü).
      - `dep_country == arr_country` (domestic) -> GEREKMEZ.
      - Schengen -> Schengen (farklı ülke) -> GEREKMEZ.
      - AKSİ HALDE (en az biri Schengen DEĞİLSE) -> MEVCUT uluslararası
        davranış KORUNUR: GEREKİR. Global çıkış/giriş kontrolleri
        ülkeye göre çok farklılaştığı için TEK bir evrensel
        "non-Schengen -> non-Schengen" kuralı VARSAYILMAZ (bkz. modül
        docstring'i).
    """
    if not dep_country or not arr_country:
        return True
    if dep_country == arr_country:
        return False
    if is_schengen_country(dep_country) and is_schengen_country(arr_country):
        return False
    return True
