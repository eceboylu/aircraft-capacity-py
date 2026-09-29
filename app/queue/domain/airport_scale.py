
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCALE_MEGA = "mega"
SCALE_LARGE = "large"
SCALE_MEDIUM = "medium"
SCALE_SMALL = "small"

SCALES = (SCALE_MEGA, SCALE_LARGE, SCALE_MEDIUM, SCALE_SMALL)

SCALE_RESOURCES: dict[str, dict[str, int]] = {
    SCALE_MEGA: {
        # ADIM (MEGA Dynamic Resource Policy) - source of truth artık
        # bunlar: passport_dep/passport_arr/security_intl'in HEPSİ
        # base'den max'a 5'lik kademelerle (ramp_step) dinamik açılıyor,
        # 5 dakikalık checkpoint ile (bkz. constants.py DEFAULT_DYNAMIC_
        # CONTROL_INTERVAL_MINUTES). Eski değerler (dep base=30/max=45,
        # arr base=35/max=45, security static=20) ARTIK KULLANILMIYOR.
        #
        # ADIM (MEGA Base Revision) - security_intl base'i 15'ten 20'ye
        # YÜKSELTİLDİ (kullanıcı talebi - "artık 15 aktif resource count
        # OLAMAZ"). passport_arr base'i (30) DEĞİŞMEDİ.
        #
        # ADIM (phpMyAdmin'den canlı güncelleme) - `departure_passport_
        # servers`/`_max` kullanıcı tarafından SQL'den (airport_scale_
        # configs.scale='mega') 30/45'e GÜNCELLENDİ ve bu Python sabiti
        # o kararı yansıtacak şekilde SENKRONİZE edildi ("ona göre de
        # kodda güncelle" talebi) - böylece bu satır ileride SIFIRDAN
        # (fresh) bir DB'de yeniden seed edilirse de AYNI (30/45) değerle
        # başlar, eski (20/40) değere GERİ DÜŞMEZ. Runtime'ın KENDİSİ zaten
        # DB'deki satırı Python sabitinin ÖNÜNE koyuyordu (bkz. `pipeline.
        # py:ensure_airport_scale_resource_config` - satır VARSA asla
        # üzerine yazılmaz) - bu değişiklik sadece "ileride sıfırdan
        # kurulan bir ortamın varsayılanı" için gerekli, MEVCUT DB
        # davranışını hiç ETKİLEMEDİ (zaten DB'deki 30/45 kullanılıyordu).
        # ADIM (Kapasite/Talep Uyumsuzluğu - 60'a Yükseltme) - kullanıcı
        # talebi: IST'e eklenen gerçekçi tam-gün tarifede passport_dep
        # günlük toplam talebi ~56.000 yolcuya, tepe saatlerde 7.700+
        # yolcu/saate ulaşıyordu; max=45 gişe * 1dk/yolcu = 2.700/saat
        # teorik tavan bunun ÇOK altında kalıp 9+ saatlik backlog'a yol
        # açıyordu (bkz. queue_predictions kanıtı - saat 15:00'te 553dk
        # bekleme). 45->60 yükseltmesi bu somut tepe-saat açığını (7.700
        # vs 2.700) kapatmak için seçildi (60*1=3.600/saat tavan, hâlâ tek
        # başına tepe saati tam karşılamaz ama backlog'un GÜNÜ AŞAN
        # birikimini önemli ölçüde azaltır - bkz. rapor).
        # `PASSPORT_DEP_OPERATIONAL_LEVELS` (constants.py) fallback'i de
        # AYNI şekilde (30/35/40/45/50/55/60) güncellendi.
        #
        # ADIM (International Arrival - Aynı Uyumsuzluk, Aynı Çözüm) -
        # passport_arr için de AYNI tespit: IST'in tam-gün tarifesinde
        # tepe saat (14:00) talebi 8.244 yolcu/saate çıkıyor, eski max=40
        # gişe * 1dk/yolcu = 2.400/saat tavan bunun ÇOK altında kalıp
        # backlog'un ERTESİ GÜNE (2026-09-30 05:00'e kadar) taşmasına yol
        # açıyordu (peak=767dk ≈ 12.8 saat). departure ile TUTARLI olacak
        # şekilde AYNI 60 tavanına yükseltildi (60*1=3.600/saat) - tepe
        # saati tek başına karşılamıyor ama backlog artık GÜN İÇİNDE
        # eriyor, ertesi güne sarkmıyor (bkz. rapor).
        # `PASSPORT_ARR_OPERATIONAL_LEVELS` (constants.py) AYNI şekilde
        # (30/35/40/45/50/55/60) güncellendi.
        "departure_passport_servers": 30,
        "departure_passport_servers_max": 60,
        "arrival_passport_servers": 30,
        "arrival_passport_servers_max": 60,
        "domestic_security_lanes": 30,
        "international_security_lanes": 20,
        "international_security_lanes_max": 40,
        # ADIM (Editable Dynamic Staffing Config) - phpMyAdmin'den
        # doğrudan görülebilsin diye NULL değil, GERÇEK varsayılan
        # değerlerle seed edilir (bkz. `pipeline.py:ensure_airport_
        # scale_resource_config`, `config.py`'nin okuma fallback zinciri
        # bunlar boş/silinmiş olsa bile AYNI sabitlere düşmeye devam
        # eder).
        "passport_departure_control_interval_minutes": 5,
        "passport_arrival_control_interval_minutes": 5,
        "dynamic_target_utilization": 0.85,
        "passport_arrival_lookahead_minutes": 20,
    },
    SCALE_LARGE: {
        # ADIM (Real-World Averages Update) - kullanıcı talebi: passport
        # (manuel) gişe sayıları 10/12'den güncel havalimanı ortalamasına
        # yükseltildi. Security lane sayıları (domestic/international=15)
        # bu talepte DEĞİŞMEDİ.
        "departure_passport_servers": 15,
        "arrival_passport_servers": 16,
        "domestic_security_lanes": 15,
        "international_security_lanes": 15,
    },
    SCALE_MEDIUM: {
        # ADIM (Real-World Averages Update) - kullanıcı talebi: "Orta
        # ölçekli havalimanı ortalaması" - Domestic Security X-ray=6 lane,
        # Intl Departure Manuel Passport=8 gişe, Intl Departure Security
        # X-ray=5 lane, Intl Arrival Manuel Passport=8 gişe.
        "departure_passport_servers": 8,
        "arrival_passport_servers": 8,
        "domestic_security_lanes": 6,
        "international_security_lanes": 5,
    },
    SCALE_SMALL: {
        # ADIM (Real-World Averages Update) - kullanıcı talebi: "SMALL
        # ortalama" - Domestic Security X-ray=3 lane, Intl Departure
        # Manuel Passport=4 gişe, Intl Departure Security X-ray=3 lane,
        # Intl Arrival Manuel Passport=4 gişe.
        "departure_passport_servers": 4,
        "arrival_passport_servers": 4,
        "domestic_security_lanes": 3,
        "international_security_lanes": 3,
    },
}

_LINE_RE = re.compile(r"^([A-Za-z0-9]{2,4})\s*/\s*([A-Za-z0-9-]{2,4})\s*-\s*(.+)$")
_NO_ICAO_TOKENS = {"", "-", "--", "---"}


def _is_header_placeholder(iata: str, icao: str) -> bool:
    return iata.upper() == "IATA" or icao.upper() == "ICAO"


@dataclass(frozen=True)
class ScaleCodeSet:

    iata_codes: frozenset[str] = field(default_factory=frozenset)
    icao_codes: frozenset[str] = field(default_factory=frozenset)


def parse_scale_list(path: str) -> ScaleCodeSet:
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
    if scale is None:
        return None
    return SCALE_RESOURCES.get(scale)
