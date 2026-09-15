r"""
flight_airports.sql -> Airport tablosu.

Kaynak bir MySQL dökümüdür; ülke/şehir/timezone bilgisi `customized`
sütunundaki JSON metninin içindedir. Burada o INSERT satırları
ayrıştırılıp SQLite'a taşınır.

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
import re

from sqlalchemy import select

from ..models import Airport

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
