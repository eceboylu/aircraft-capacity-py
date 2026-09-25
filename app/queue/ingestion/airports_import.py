r"""
flight_airports.sql -> Airport tablosu.

Kaynak bir MySQL dökümüdür; ülke/şehir/timezone bilgisi `customized`
sütunundaki JSON metninin içindedir. Burada o INSERT satırları
ayrıştırılıp `DATABASE_URL`'in gösterdiği (bu proje MySQL-only - bkz.
`app/db.py`) veritabanına yazılır.

Bir kere çalıştırılır; tekrar çalıştırılırsa merge() ile üzerine
yazar, yeni satır açmaz (YASAK 4).

MADDE 6 - SQL string'i ile JSON string'i KARIŞTIRILMAZ:
`customized` sütunu, SQL tarafından bir kere escape edilmiş JSON
metnidir (JSON'un kendisi de ayrıca kendi kaçış kurallarına sahiptir,
örn. `\/`, `\n`). `_sql_unescape()` SADECE SQL katmanını çözer - yani
dump'ın kattığı escaping'i (`\'`, `\\`, `\"`, `\n`, `\r`, `\0`) TEK
GEÇİŞTE geri alır. JSON'un KENDİ kaçışları (`\/`, `\n` bir JSON string
içindeyse) bu katmana hiç dokunulmadan `json.loads()`'a bırakılır.

Bunun neden önemli olduğu: gerçek dosyada zaman dilimi gibi alanlar
JSON tarafından `\/` ile kaçırılmış slash içeriyor (örn.
"Pacific\/Port_Moresby"). Dump bu değerin İÇİNDEKİ backslash'ı SQL
seviyesinde AYRICA escape ettiği için ham SQL metninde bu segment
`\\/ ` (2 backslash + slash) olarak görünür. Sıralı/ayrı `.replace()`
çağrıları (önceki uygulama) bunu YANLIŞ çözebilir: `\\`'yi tek
backslash'a indirip SONRA kalan `\n`'i (bambaşka bir bağlamda, örn.
JSON'un kendi `\n` kaçışının backslash'ı `\\` haline gelmiş kalıntısı)
gerçek bir newline karakterine çevirebilir - bu da `json.loads()`'u
"Invalid control character" hatasıyla kırar. Tek geçişli regex
(`\\(.)`, soldan sağa, üst üste binmeyen eşleşmeler) bu belirsizliği
doğru çözer: `\\` + herhangi bir karakter HER ZAMAN TEK bir birim
olarak tüketilir, üretilen çıktı yeniden taranmaz.
"""

import json
import logging
import re

from sqlalchemy import select

from ..domain.airport_scale import (
    find_cross_scale_conflicts,
    parse_scale_list,
    resolve_airport_scale,
)
from ..models import Airport

logger = logging.getLogger(__name__)

# ('IATA', 'ICAO', 'Ad', '{json}') dörtlüsünü yakalar.
# Her alan hem \x (backslash-escape) hem '' (doubled-quote escape,
# NO_BACKSLASH_ESCAPES modu) tarzını "string burada bitmiyor" olarak
# tanır - bu dosya sadece backslash tarzı kullanıyor ama parser'ı
# başka bir dump biçimine de dayanıklı kılmak için ikisi de tanınır.
_VALUES_ROW = re.compile(
    r"\((\d+),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*'),\s*"
    r"(NULL|'(?:[^'\\]|\\.|'')*')\)",
    re.DOTALL,
)

# MySQL'in backslash-escape tablosu (bkz. MySQL docs "String Literals").
# Tabloda olmayan bir \X dizisi için MySQL kuralı: backslash yok
# sayılır, karakter olduğu gibi kalır (ör. \/ -> /).
_SQL_ESCAPE_MAP = {
    "'": "'",
    '"': '"',
    "\\": "\\",
    "n": "\n",
    "r": "\r",
    "0": "\0",
    "t": "\t",
}
_ESCAPE_PAIR = re.compile(r"\\(.)", re.DOTALL)


def _sql_unescape(value: str) -> str:
    """
    SADECE SQL katmanının backslash-escape'ini tek geçişte çözer.

    JSON'un kendi kaçış kurallarına (`\\/`, `\\n` bir JSON string
    içindeyse) KESİNLİKLE dokunmaz - onlar json.loads()'a bırakılır.
    """
    return _ESCAPE_PAIR.sub(
        lambda m: _SQL_ESCAPE_MAP.get(m.group(1), m.group(1)), value
    )


def _unquote(value: str) -> str | None:
    """SQL string literalini Python metnine çevirir."""
    if value is None or value == "NULL":
        return None
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    # '' (doubled-quote) tarzı escape - bu dosyada gerçek veri içinde
    # görülmüyor (yalnızca boş string '' vakası var, o zaten yukarıda
    # dilim [1:-1] ile boşalır), ama farklı bir dump modu için
    # dayanıklılık amaçlı tutulur.
    value = value.replace("''", "'")
    return _sql_unescape(value)


def parse_airports_sql(path: str) -> list[dict]:
    """SQL dökümündeki havalimanı satırlarını sözlüğe çevirir."""
    with open(path, encoding="utf-8") as handle:
        content = handle.read()

    airports: list[dict] = []
    seen: set[str] = set()

    for match in _VALUES_ROW.finditer(content):
        _, iata_raw, icao_raw, name_raw, customized_raw = match.groups()

        iata = (_unquote(iata_raw) or "").strip().upper()
        if not iata or iata in seen:
            continue

        details = {}
        customized = _unquote(customized_raw)
        if customized:
            try:
                details = json.loads(customized)
            except (ValueError, TypeError):
                details = {}

        seen.add(iata)
        airports.append({
            "iata_code": iata,
            "icao_code": (_unquote(icao_raw) or "").strip().upper() or None,
            "airport_name": _unquote(name_raw),
            "city_code": (details.get("city_code") or "").strip().upper() or None,
            "country_code": (details.get("country_code") or "").strip().upper() or None,
            "timezone": details.get("timezone") or None,
        })

    return airports


def import_airports(session, path: str) -> int:
    """Havalimanlarını upsert eder, işlenen satır sayısını döndürür."""
    rows = parse_airports_sql(path)
    for row in rows:
        session.merge(Airport(**row))
    session.commit()
    return len(rows)


def _parse_and_check_conflicts(
    mega_path: str, large_path: str, medium_path: str, small_path: str,
) -> tuple[dict, dict[str, dict[str, set[str]]], set[str], set[str]]:
    """
    4 ölçek dosyasını parse edip cross-scale conflict'leri hesaplar -
    `import_airport_scales()`/`refresh_airport_scales()` arasında
    paylaşılan ORTAK adım (iki fonksiyon SADECE "eşleşmeyen airport'a
    ne olur" davranışında farklılaşır, parse/conflict mantığı AYNI).
    """
    scales = {
        "mega": parse_scale_list(mega_path),
        "large": parse_scale_list(large_path),
        "medium": parse_scale_list(medium_path),
        "small": parse_scale_list(small_path),
    }

    conflicts = find_cross_scale_conflicts(
        scales["mega"], scales["large"], scales["medium"], scales["small"],
    )
    conflicting_iata: set[str] = set()
    for codes in conflicts["iata"].values():
        conflicting_iata |= codes
    conflicting_icao: set[str] = set()
    for codes in conflicts["icao"].values():
        conflicting_icao |= codes

    if conflicting_iata or conflicting_icao:
        logger.warning(
            "airport scale kaynak dosyalarında çakışma bulundu (%d IATA, %d ICAO) - "
            "bu kodlar için scale=None bırakıldı (sessizce SEÇİLMEDİ): iata=%s icao=%s",
            len(conflicting_iata), len(conflicting_icao),
            sorted(conflicting_iata), sorted(conflicting_icao),
        )

    return scales, conflicts, conflicting_iata, conflicting_icao


def import_airport_scales(
    session, mega_path: str, large_path: str, medium_path: str, small_path: str,
) -> dict:
    """
    ADIM (4-Tier Airport Scale) - 4 ölçek txt dosyasını (mega/large/
    medium/small) ayrıştırıp `Airport.scale`'i doldurur.

    Bu fonksiyon İLK/BOŞ bootstrap içindir (bkz. `pipeline.py:ensure_
    airport_scales()` - "hiç `Airport.scale` set edilmemiş" durumu):
    session'daki TÜM `Airport` satırları için scale KOŞULSUZ yazılır -
    eşleşme yoksa `None` (çakışma varsa da `None`). Zaten çözülmüş bir
    veritabanını YENİ bir contract'a göre GÜNCELLEMEK için bunun
    yerine `refresh_airport_scales()` kullanılmalı (o, eşleşmeyen
    airport'ların MEVCUT scale'ini KORUR - bkz. o fonksiyonun
    docstring'i).

    `import_airports()` İLE AYNI güvenlik ilkesi: mevcut `Airport`
    satırlarının DİĞER alanları (icao/name/city/country/timezone) HİÇ
    DOKUNULMAZ - `session.merge(Airport(...))` KULLANILMAZ (yeni bir
    Airport nesnesi merge etmek, ORM'de ayarlanmayan alanları Python
    varsayılanlarıyla - yani None ile - EZERDİ). Bunun yerine mevcut
    satırlar SORGULANIP SADECE `.scale` alanı güncellenir.

    Çakışma güvenliği: bir IATA/ICAO kodu BİRDEN FAZLA ölçek dosyasında
    görünüyorsa (`find_cross_scale_conflicts`), o kod için SESSİZCE bir
    ölçek SEÇİLMEZ - `scale=None` kalır (log ile açıkça uyarılır, bkz.
    dönen özetin `iata_conflicts`/`icao_conflicts` alanları).

    Her prediction turunda ÇAĞRILMAZ - bir kereye mahsus/idempotent
    bootstrap adımıdır (bkz. `pipeline.py:ensure_airport_scales`).
    """
    scales, conflicts, conflicting_iata, conflicting_icao = _parse_and_check_conflicts(
        mega_path, large_path, medium_path, small_path,
    )

    airports = session.execute(select(Airport)).scalars().all()
    matched = 0
    unmatched = 0
    conflicted = 0

    for airport in airports:
        iata = (airport.iata_code or "").upper()
        icao = (airport.icao_code or "").upper()
        if iata in conflicting_iata or (icao and icao in conflicting_icao):
            airport.scale = None
            conflicted += 1
            continue

        scale = resolve_airport_scale(
            airport.iata_code, airport.icao_code,
            scales["mega"], scales["large"], scales["medium"], scales["small"],
        )
        airport.scale = scale
        if scale is not None:
            matched += 1
        else:
            unmatched += 1

    session.commit()

    return {
        "airports_checked": len(airports),
        "matched": matched,
        "unmatched": unmatched,
        "conflicted": conflicted,
        "iata_conflicts": {key: sorted(codes) for key, codes in conflicts["iata"].items()},
        "icao_conflicts": {key: sorted(codes) for key, codes in conflicts["icao"].items()},
    }


def refresh_airport_scales(
    session, mega_path: str, large_path: str, medium_path: str, small_path: str,
) -> dict:
    """
    ADIM (4-Tier Airport Scale - MySQL Data Refresh) - `import_airport_
    scales()`'ten FARKLI olarak, ZATEN scale'i çözülmüş (ör. eski 3-tier
    import'tan gelen) bir veritabanını YENİ 4-tier contract'a göre
    GÜVENLE günceller:

      - Bir airport GÜNCEL 4 listeden BİRİNDE bulunuyorsa -> `.scale`
        o listenin tier'ına YAZILIR (eskiden "large" olan bir havalimanı
        artık "mega" listesindeyse `.scale = "mega"` olur - bu fonksiyon
        BUNUN İÇİN VAR).
      - Çakışma varsa (aynı kod 2+ listede) -> `.scale = None` (uydurma
        seçim YOK, `import_airport_scales()` ile AYNI güvenlik ilkesi).
      - Airport HİÇBİR listede bulunamıyorsa -> `.scale`'e HİÇ
        DOKUNULMAZ, mevcut değeri NE İSE o kalır (görev talimatı:
        "Scale listesinde olmayan airport'lara mevcut contract ne
        diyorsa onu koru; rastgele scale atama"). `import_airport_
        scales()`'ten TEK YAPISAL FARK budur - o fonksiyon eşleşmeyeni
        KOŞULSUZ `None`'a ÇEVİRİR (ilk/boş bootstrap için doğru), bu
        fonksiyon İSE bir "refresh" olduğu için var olan bilgiyi
        gereksiz yere SİLMEZ.

    IDEMPOTENT: aynı 4 dosya ile ikinci kez çalıştırıldığında hiçbir
    satır DEĞİŞMEZ (`.scale` zaten aynı değere yeniden atanır, yeni
    satır AÇILMAZ - `Airport.iata_code` primary key, bu fonksiyon
    SADECE var olan satırları günceller).
    """
    scales, conflicts, conflicting_iata, conflicting_icao = _parse_and_check_conflicts(
        mega_path, large_path, medium_path, small_path,
    )

    airports = session.execute(select(Airport)).scalars().all()
    updated = 0
    unchanged = 0
    conflicted = 0
    preserved_unmatched = 0

    for airport in airports:
        iata = (airport.iata_code or "").upper()
        icao = (airport.icao_code or "").upper()
        if iata in conflicting_iata or (icao and icao in conflicting_icao):
            if airport.scale is not None:
                airport.scale = None
                updated += 1
            conflicted += 1
            continue

        scale = resolve_airport_scale(
            airport.iata_code, airport.icao_code,
            scales["mega"], scales["large"], scales["medium"], scales["small"],
        )
        if scale is None:
            # Hiçbir listede yok - mevcut değeri KORU (rastgele/uydurma
            # scale atama YOK).
            preserved_unmatched += 1
            continue

        if airport.scale != scale:
            airport.scale = scale
            updated += 1
        else:
            unchanged += 1

    session.commit()

    return {
        "airports_checked": len(airports),
        "updated": updated,
        "unchanged": unchanged,
        "conflicted": conflicted,
        "preserved_unmatched": preserved_unmatched,
        "iata_conflicts": {key: sorted(codes) for key, codes in conflicts["iata"].items()},
        "icao_conflicts": {key: sorted(codes) for key, codes in conflicts["icao"].items()},
    }


def country_lookup(session) -> dict[str, str]:
    """
    {IATA: ülke kodu} sözlüğü.

    location (domestic/international) türetimi bunun üzerinden yapılır;
    her uçuş için ayrı sorgu atılmaz.
    """
    rows = session.execute(
        select(Airport.iata_code, Airport.country_code)
        .where(Airport.country_code.isnot(None))
    ).all()
    return {iata: country for iata, country in rows if iata and country}
