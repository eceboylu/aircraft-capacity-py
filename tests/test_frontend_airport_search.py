"""
ADIM (Frontend Airport Search + Mobile Selector) - Section AA/AB
doğrulaması.

`index.html`'in GERÇEK arama/dropdown JS mantığı, `tests/frontend_
harness/run_search.js` üzerinden bir Node motorunda GERÇEKTEN
ÇALIŞTIRILARAK doğrulanır (string-match DEĞİL - `test_frontend_4_graph_
contract.py`'nin statik desenini TAMAMLAYICI, davranışsal bir katman).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "app" / "web" / "static" / "index.html"
HARNESS = Path(__file__).resolve().parent / "frontend_harness" / "run_search.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node.js bu ortamda kurulu değil")

DIRECTORY = [
    {"iata": "IST", "name": "Istanbul Airport"},
    {"iata": "SAW", "name": "Istanbul Sabiha Gokcen International Airport"},
    {"iata": "ADB", "name": "Izmir Adnan Menderes Airport"},
    {"iata": "CDG", "name": "Charles de Gaulle Airport"},
    {"iata": "FRA", "name": "Frankfurt Airport"},
    {"iata": "OAG", "name": "Orange Airport"},
]


def _run(tmp_path, actions, viewport="desktop"):
    scenario = {"directory": DIRECTORY, "actions": actions, "viewport": viewport}
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(HARNESS), str(INDEX_HTML), str(scenario_path)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "THREW" not in result.stdout, result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------
# AA - Search testleri.
# ------------------------------------------------------------------

def test_search_ist_finds_istanbul_airport(tmp_path):
    out = _run(tmp_path, [{"type": "setQuery", "value": "IST"}])
    assert "IST" in out["filtered"]


def test_search_lowercase_istanbul_finds_ist_and_saw(tmp_path):
    out = _run(tmp_path, [{"type": "setQuery", "value": "istanbul"}])
    assert "IST" in out["filtered"]
    assert "SAW" in out["filtered"]


def test_search_saw_finds_saw(tmp_path):
    out = _run(tmp_path, [{"type": "setQuery", "value": "SAW"}])
    assert out["filtered"] == ["SAW"]


def test_search_partial_frank_finds_fra(tmp_path):
    out = _run(tmp_path, [{"type": "setQuery", "value": "Frank"}])
    assert out["filtered"] == ["FRA"]


def test_search_is_case_insensitive_for_mixed_case_query(tmp_path):
    out = _run(tmp_path, [{"type": "setQuery", "value": "fRa"}])
    assert out["filtered"] == ["FRA"]


def test_search_turkish_uppercase_i_matches_istanbul(tmp_path):
    """Türkçe noktasız/noktalı İ/I normalize edilir - 'İSTANBUL' de eşleşmeli."""
    out = _run(tmp_path, [{"type": "setQuery", "value": "İSTANBUL"}])
    assert "IST" in out["filtered"]
    assert "SAW" in out["filtered"]


def test_empty_query_shows_full_directory(tmp_path):
    out = _run(tmp_path, [{"type": "setQuery", "value": ""}])
    assert set(out["filtered"]) == {e["iata"] for e in DIRECTORY}


def test_no_match_query_shows_no_results_message(tmp_path):
    out = _run(tmp_path, [{"type": "setQuery", "value": "zzz_no_such_airport"}])
    assert out["filtered"] == []
    assert "sonuç bulunamadı" in out["listHtml"]


# ------------------------------------------------------------------
# G/H/I - Dropdown open/close + selection.
# ------------------------------------------------------------------

def test_opening_selector_sets_open_state_and_class(tmp_path):
    out = _run(tmp_path, [{"type": "openSelector"}])
    assert out["selectorOpen"] is True
    assert out["selectorOpenClass"] is True


def test_mobile_selecting_airport_closes_dropdown_and_sets_selection(tmp_path):
    """ADIM (Desktop Always Open) NOTU: bu kapanma davranışı ARTIK SADECE mobilde - bkz. desktop testleri altta."""
    out = _run(tmp_path, [
        {"type": "openSelector"},
        {"type": "setQuery", "value": "IST"},
        {"type": "selectAirport", "iata": "IST"},
    ], viewport="mobile")
    assert out["selected"] == "IST"
    assert out["selectorOpen"] is False
    assert out["selectorOpenClass"] is False
    assert out["searchQuery"] == ""   # kapanınca arama metni temizlenir


def test_mobile_reselecting_same_airport_still_closes_dropdown(tmp_path):
    """selectAirport(aynı iata) erken return ETMEMELİ - mobilde dropdown hâlâ kapanmalı."""
    out = _run(tmp_path, [
        {"type": "selectAirport", "iata": "IST"},
        {"type": "openSelector"},
        {"type": "selectAirport", "iata": "IST"},
    ], viewport="mobile")
    assert out["selected"] == "IST"
    assert out["selectorOpen"] is False


# ------------------------------------------------------------------
# ADIM (Desktop Airport Selector Always Open) - masaüstünde liste
# seçimden SONRA AÇIK kalır, arama metni KORUNUR.
# ------------------------------------------------------------------

def test_desktop_selecting_airport_keeps_list_open_and_query(tmp_path):
    out = _run(tmp_path, [
        {"type": "openSelector"},
        {"type": "setQuery", "value": "IST"},
        {"type": "selectAirport", "iata": "IST"},
    ], viewport="desktop")
    assert out["selected"] == "IST"
    assert out["searchQuery"] == "IST"   # masaüstünde arama metni TEMİZLENMEZ
    assert not out["isMobileViewport"]


def test_desktop_can_switch_between_airports_without_reopening(tmp_path):
    out = _run(tmp_path, [
        {"type": "selectAirport", "iata": "IST"},
        {"type": "selectAirport", "iata": "SAW"},
        {"type": "selectAirport", "iata": "CDG"},
    ], viewport="desktop")
    assert out["selected"] == "CDG"




def test_closing_without_selection_clears_query_but_not_selection(tmp_path):
    out = _run(tmp_path, [
        {"type": "selectAirport", "iata": "CDG"},
        {"type": "openSelector"},
        {"type": "setQuery", "value": "zzz"},
        {"type": "closeSelector"},
    ])
    assert out["selected"] == "CDG"      # vazgeçme - seçim DEĞİŞMEDİ
    assert out["selectorOpen"] is False
    assert out["searchQuery"] == ""


def test_selected_label_shows_iata_and_name_when_closed(tmp_path):
    out = _run(tmp_path, [{"type": "selectAirport", "iata": "FRA"}])
    assert "FRA" in out["labelHtml"]
    assert "Frankfurt" in out["labelHtml"]


def test_touch_friendly_item_height_present_in_css():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "min-height: 44px" in html   # Bölüm H - touch-friendly hedef


def test_mobile_media_query_limits_list_height_for_scroll():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "max-width: 760px" in html
    assert "overflow-y: auto" in html


# ------------------------------------------------------------------
# CSS - masaüstünde her zaman görünür, mobilde toggle DEĞİŞMEDİ.
# ------------------------------------------------------------------

def test_css_always_shows_search_and_list_above_760px():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "@media (min-width: 761px)" in html
    desktop_block = html.split("@media (min-width: 761px)", 1)[1].split("}\n\n", 1)[0]
    assert "#airport-search { display: block" in desktop_block
    assert "#airport-list { display: block" in desktop_block


def test_css_mobile_toggle_rules_still_present_unchanged():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert '#airport-selector.open #airport-search { display: block; }' in html
    assert '#airport-selector.open #airport-list { display: block; }' in html
    assert "@media (max-width: 760px)" in html


def test_is_mobile_viewport_helper_uses_same_760px_breakpoint_as_css():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'window.matchMedia("(max-width: 760px)")' in html
