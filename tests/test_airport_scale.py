"""
PHASE 1 - `app/queue/domain/airport_scale.py` saf parser/resolver
testleri. Gerçek `data/*.txt` dosyalarına karşı çalışır (salt okunur -
hiçbir dosya değiştirilmez).
"""
import os

import pytest

from app.queue.domain.airport_scale import (
    SCALE_LARGE,
    SCALE_MEDIUM,
    SCALE_RESOURCES,
    SCALE_SMALL,
    ScaleCodeSet,
    find_cross_scale_conflicts,
    find_duplicate_iata,
    parse_scale_list,
    resolve_airport_scale,
    resource_view_for_scale,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
LARGE_PATH = os.path.join(DATA_DIR, "buyuk_olcekli_havaalanlari.txt")
MEDIUM_PATH = os.path.join(DATA_DIR, "orta_olcekli_havaalanlari.txt")
SMALL_PATH = os.path.join(DATA_DIR, "kucuk_olcekli_havaalanlari.txt")


@pytest.fixture(scope="module")
def large():
    return parse_scale_list(LARGE_PATH)


@pytest.fixture(scope="module")
def medium():
    return parse_scale_list(MEDIUM_PATH)


@pytest.fixture(scope="module")
def small():
    return parse_scale_list(SMALL_PATH)


# ========================================================================
# Parser - header'lar airport sayılmıyor, `---` geçerli ICAO değil.
# ========================================================================

def test_large_parser_count_matches_declared_total(large):
    # Dosyanın kendi "Toplam: 1.138" başlığıyla BİREBİR aynı.
    assert len(large.iata_codes) == 1138


def test_medium_parser_count_matches_declared_total(medium):
    assert len(medium.iata_codes) == 2056


def test_small_parser_count_matches_declared_total(small):
    assert len(small.iata_codes) == 740


def test_header_lines_are_not_counted_as_airports(large):
    # "Biçim: IATA / ICAO - Havaalanı adı" satırı literal "IATA"/"ICAO"
    # kod olarak İÇERİ SIZMAMALI.
    assert "IATA" not in large.iata_codes
    assert "ICAO" not in large.icao_codes


def test_dash_icao_is_not_a_valid_icao_code(large, medium, small):
    for codes in (large, medium, small):
        assert "---" not in codes.icao_codes
        assert "-" not in codes.icao_codes


def test_known_real_entries_parsed_correctly(large):
    # Gerçek dosyadan doğrulanmış örnekler.
    assert "GUM" in large.iata_codes  # A.B. Won Pat International Airport
    assert "PGUM" in large.icao_codes
    assert "ZRH" in large.iata_codes
    assert "LSZH" in large.icao_codes


def test_dash_icao_airport_keeps_iata_but_not_icao(medium):
    assert "BKN" in medium.iata_codes
    assert "BKN" not in medium.icao_codes  # IATA kodu ICAO kümesine SIZMAMALI


# ========================================================================
# Duplicate / cross-scale conflict denetimi (Bölüm 3 talimatı: sessizce
# seçim YAPMA, açıkça raporla).
# ========================================================================

def test_no_duplicate_iata_within_each_scale_file():
    assert find_duplicate_iata(LARGE_PATH) == []
    assert find_duplicate_iata(MEDIUM_PATH) == []
    assert find_duplicate_iata(SMALL_PATH) == []


def test_no_cross_scale_iata_or_icao_conflicts(large, medium, small):
    conflicts = find_cross_scale_conflicts(large, medium, small)
    assert conflicts["iata"] == {}, f"IATA conflicts found: {conflicts['iata']}"
    assert conflicts["icao"] == {}, f"ICAO conflicts found: {conflicts['icao']}"


def test_find_cross_scale_conflicts_actually_detects_synthetic_overlap():
    """Denetim fonksiyonunun KENDİSİNİN çalıştığını, sadece gerçek verinin temiz olduğunu kanıtlamıyor - sentetik çakışmayla ayrıca doğrula."""
    large_fake = ScaleCodeSet(iata_codes=frozenset({"AAA", "BBB"}), icao_codes=frozenset({"XAAA"}))
    medium_fake = ScaleCodeSet(iata_codes=frozenset({"BBB", "CCC"}), icao_codes=frozenset({"XAAA"}))
    small_fake = ScaleCodeSet(iata_codes=frozenset(), icao_codes=frozenset())
    conflicts = find_cross_scale_conflicts(large_fake, medium_fake, small_fake)
    assert conflicts["iata"]["large&medium"] == {"BBB"}
    assert conflicts["icao"]["large&medium"] == {"XAAA"}


# ========================================================================
# IATA lookup / ICAO fallback / resource mapping.
# ========================================================================

def test_resolve_by_iata_exact_match(large, medium, small):
    assert resolve_airport_scale("ZRH", "WRONGICAO", large, medium, small) == SCALE_LARGE


def test_resolve_falls_back_to_icao_when_iata_not_found(large, medium, small):
    # IATA hiçbir listede yoksa ama ICAO large'daysa -> large.
    assert resolve_airport_scale("ZZZZNOTFOUND", "LSZH", large, medium, small) == SCALE_LARGE


def test_resolve_returns_none_for_completely_unknown_airport(large, medium, small):
    assert resolve_airport_scale("ZZZZ", "ZZZZ", large, medium, small) is None


def test_resolve_returns_none_when_no_codes_given(large, medium, small):
    assert resolve_airport_scale(None, None, large, medium, small) is None


def test_resource_mapping_large_medium_small():
    """
    ADIM (Generic Scale Resource Update) - değerler gerçek dünya
    kanıtına göre güncellendi (bkz. rapor: IST 68 departure passport
    gişesi, SIN ~130 otomatik immigration lane - eski 20/30/22/22
    mega-hub'lar için çok düşük kalıyordu). Alan adları/mapping AYNI.
    """
    assert resource_view_for_scale(SCALE_LARGE) == {
        "departure_passport_servers": 30,
        "arrival_passport_servers": 45,
        "domestic_security_lanes": 28,
        "international_security_lanes": 18,
    }
    assert resource_view_for_scale(SCALE_MEDIUM) == {
        "departure_passport_servers": 10,
        "arrival_passport_servers": 15,
        "domestic_security_lanes": 12,
        "international_security_lanes": 6,
    }
    assert resource_view_for_scale(SCALE_SMALL) == {
        "departure_passport_servers": 3,
        "arrival_passport_servers": 4,
        "domestic_security_lanes": 4,
        "international_security_lanes": 2,
    }


def test_resource_mapping_unknown_scale_returns_none():
    assert resource_view_for_scale(None) is None
    assert resource_view_for_scale("gigantic") is None


def test_scale_resources_constant_has_exactly_three_scales():
    assert set(SCALE_RESOURCES) == {SCALE_LARGE, SCALE_MEDIUM, SCALE_SMALL}
