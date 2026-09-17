"""
ADIM (Frontend 4-Graph Contract) - genel-proje.md Bölüm 26/27/50/51/
62/63/64/67 doğrulaması.

`index.html` pytest ile ÇALIŞTIRILAMAZ (JS runtime yok) - bu dosya
mevcut `test_operational_4_graph_e2e.py`/`test_operational_4_graph_
frontend.py`'deki desenle AYNI: kaynağı STATİK olarak ayrıştırıp
(fonksiyon imzaları, çağrı sırası, sabitler) davranış GARANTİLERİNİ
doğrular - gerçek bir tarayıcıda çalıştırmadan.

Backend/engine matematiği bu ADIM'da DEĞİŞTİRİLMEDİ - burada test
edilen SADECE frontend'in API'den GELEN hazır sonucu doğru okuyup
okumadığı (hiçbir YENİ hesap yapmadan).
"""

from pathlib import Path

INDEX_HTML = Path(__file__).parents[1] / "app" / "web" / "static" / "index.html"


def _html():
    return INDEX_HTML.read_text(encoding="utf-8")


def _script():
    return _html().split("<script>", 1)[1].split("</script>", 1)[0]


# ========================================================================
# Bölüm 15/16/26 - TAM 4 ana grafik, beşinci ("domestic arrival") yok.
# ========================================================================

def test_render_content_wires_exactly_the_four_required_graph_keys():
    script = _script()
    render_fn = script.split("function renderContent() {", 1)[1].split("\n  function ", 1)[0]

    for key, data_field in (
        ("overall", "data.overall"),
        ("domestic_security", "data.domestic_security"),
        ("international_departure", "data.international_departure"),
        ("international_arrival", "data.international_arrival"),
    ):
        assert 'graphSectionHtml("' + key + '"' in render_fn
        assert data_field in render_fn

    # Beşinci ("domestic arrival"/"domestic_arrival") bir grafik YOK.
    assert "domestic_arrival" not in render_fn
    assert render_fn.count("graphSectionHtml(") == 4
    assert render_fn.count("drawChart(") == 4


def test_international_departure_uses_breakdown_kind_others_use_single():
    script = _script()
    render_fn = script.split("function renderContent() {", 1)[1].split("\n  function ", 1)[0]

    assert 'graphSectionHtml("international_departure", "INTERNATIONAL DEPARTURE", data.international_departure, "breakdown")' in render_fn
    assert 'graphSectionHtml("overall", "GENEL HAVALİMANI YOĞUNLUĞU", data.overall, "single")' in render_fn
    assert 'graphSectionHtml("domestic_security", "DOMESTIC SECURITY", data.domestic_security, "single")' in render_fn
    assert 'graphSectionHtml("international_arrival", "INTERNATIONAL ARRIVAL", data.international_arrival, "single")' in render_fn


# ========================================================================
# Bölüm 50/64/67 - her grafiğin selected-hour state'i BAĞIMSIZ, refresh
# sonrası (index değil window_start ile) korunur.
# ========================================================================

def test_state_detail_has_four_independent_keys_initialized_to_null():
    script = _script()
    detail_block = script.split("detail: {", 1)[1].split("}", 1)[0]
    for key in ("overall", "domestic_security", "international_departure", "international_arrival"):
        assert key + ": null" in detail_block
    # Eski (artık geçersiz) anahtarlar YOK.
    assert "international_security:" not in detail_block
    assert "international_passport:" not in detail_block


def test_hour_click_stores_window_start_not_array_index():
    """
    Bölüm 64: refresh sonrası pencere listesi değişse bile AYNI SAAT
    bulunabilsin diye tıklanan pencerenin `window_start`'ı saklanır -
    array INDEX değil (index, pencere listesi kaysa YANLIŞ saati
    gösterebilirdi).
    """
    script = _script()
    on_click = script.split("onClick: function (evt, elements) {", 1)[1].split("\n        },", 1)[0]
    assert "state.detail[key] = windows[elements[0].index].window_start;" in on_click
    assert "state.detail[key] = elements[0].index;" not in on_click


def test_graph_section_html_looks_up_window_by_matching_window_start():
    script = _script()
    graph_fn = script.split("function graphSectionHtml(key, title, section, kind) {", 1)[1].split("\n  }", 1)[0]

    assert "state.detail[key]" in graph_fn
    assert "windows[i].window_start === selectedStart" in graph_fn
    # Bulunamazsa (prune edildi/hiç yok) current'a güvenli düşüş.
    assert "section.current" in graph_fn


def test_each_graph_click_handler_is_scoped_to_its_own_key_closure():
    """
    Bölüm 67: 10.00'a tıklanınca 11.00 wait'i gösterilmemeli - her
    `drawChart(...)` çağrısı KENDİ `key`'ini onClick closure'ına
    taşıyor (aynı elemanın index'i başka bir grafiğin state'ini
    ASLA etkilemez, çünkü `state.detail[key]` her çağrıda FARKLI bir
    `key` ile indexleniyor).
    """
    script = _script()
    render_fn = script.split("function renderContent() {", 1)[1].split("\n  function ", 1)[0]
    assert 'drawChart(data.overall.windows, "chart-overall", "overall")' in render_fn
    assert 'drawChart(data.domestic_security.windows, "chart-domestic_security", "domestic_security")' in render_fn
    assert 'drawChart(data.international_departure.windows, "chart-international_departure", "international_departure")' in render_fn
    assert 'drawChart(data.international_arrival.windows, "chart-international_arrival", "international_arrival")' in render_fn


# ========================================================================
# Bölüm 50/51/67 - International Departure: passport/security wait AYRI,
# sahte toplama/ortalama YOK.
# ========================================================================

def test_wait_detail_html_shows_passport_and_security_separately_for_breakdown():
    script = _script()
    fn = script.split("function waitDetailHtml(kind, displayWindow) {", 1)[1].split("\n  }", 1)[0]

    assert 'kind === "breakdown"' in fn
    assert "Passport bekleme" in fn
    assert "Security bekleme" in fn
    assert "displayWindow.passport" in fn
    assert "displayWindow.international_security" in fn

    # Journey-level SAHTE bir toplama/ortalama YOK (iki wait TOPLANMAZ/
    # ORTALANMAZ - waitTimeText() ayrı ayrı, ham backend değerine çağrılır).
    for forbidden in (
        "passport.estimated_wait_minutes +", "+ displayWindow.international_security",
        "/ 2", "average", "ortalama",
    ):
        assert forbidden not in fn


def test_wait_detail_html_single_kind_uses_generic_wait_line():
    script = _script()
    fn = script.split("function waitDetailHtml(kind, displayWindow) {", 1)[1].split("\n  }", 1)[0]
    assert "Tahmini bekleme süresi" in fn
    assert "waitTimeText(displayWindow)" in fn


def test_wait_time_text_never_computes_new_queue_math():
    """Frontend hiçbir Erlang-C/backlog/lambda-mu hesabı YAPMAZ - sadece backend'in ZATEN verdiği alanları okur."""
    script = _script()
    fn = script.split("function waitTimeText(w) {", 1)[1].split("\n  }", 1)[0]
    for forbidden in ("Erlang", "erlang_c", "* mu", "lam /", "backlog", "Math.exp", "factorial"):
        assert forbidden not in fn


# ========================================================================
# Bölüm 27/50/67 - null/empty/error durumları sayfayı bozmaz.
# ========================================================================

def test_missing_window_shows_safe_no_data_text_not_crash():
    script = _script()
    fn = script.split("function waitDetailHtml(kind, displayWindow) {", 1)[1].split("\n  }", 1)[0]
    assert "if (!displayWindow)" in fn
    assert "Bu saat için veri yok" in fn


def test_estimated_wait_null_shows_hesaplanamiyor_not_crash():
    script = _script()
    fn = script.split("function waitTimeText(w) {", 1)[1].split("\n  }", 1)[0]
    assert 'return "Hesaplanamıyor";' in fn
    assert "if (!w) return" in fn   # w=null/undefined güvenli ele alınır


def test_empty_windows_list_shows_placeholder_not_crash():
    script = _script()
    graph_fn = script.split("function graphSectionHtml(key, title, section, kind) {", 1)[1].split("\n  }", 1)[0]
    assert "windows.length" in graph_fn
    assert "chart-empty" in graph_fn


def test_frontend_never_renders_passenger_or_flight_counts():
    script = _script()
    graph_fn = script.split("function graphSectionHtml(key, title, section, kind) {", 1)[1].split("\n  }", 1)[0]
    wait_fn = script.split("function waitDetailHtml(kind, displayWindow) {", 1)[1].split("\n  }", 1)[0]
    for forbidden in ("expected_passengers", "flight_count"):
        assert forbidden not in graph_fn
        assert forbidden not in wait_fn


# ========================================================================
# Bölüm 26/52 - 5 dakikalık polling, queue window'dan bağımsız.
# ========================================================================

def test_poll_interval_is_five_minutes():
    html = _html()
    assert "var POLL_MS = 5 * 60 * 1000;" in html
    assert "setInterval(loadAll, POLL_MS)" in html
    assert "5 dk sonra yenilenecek" in html


def test_poll_does_not_call_any_external_source_directly():
    """`loadAll()` SADECE kendi backend API'sini (DB'den okuyan) çağırır - dış AirLabs kaynağına HİÇ çıkmaz."""
    script = _script()
    load_all = script.split("function loadAll() {", 1)[1].split("\n  }", 1)[0]
    assert "/api/airports/directory" in load_all
    assert "/api/airports/" in load_all
    for forbidden in ("airlabs", "AirLabs", "http://", "https://api"):
        assert forbidden not in load_all


# ========================================================================
# Bölüm 64 - refresh sonrası seçili airport/saat korunur.
# ========================================================================

def test_load_all_never_resets_selected_airport_or_detail_state():
    script = _script()
    load_all = script.split("function loadAll() {", 1)[1].split("\n  }", 1)[0]
    assert "state.selected = " not in load_all
    assert "state.detail" not in load_all


def test_reconcile_selection_only_changes_selection_when_it_becomes_invalid():
    script = _script()
    fn = script.split("function reconcileSelection() {", 1)[1].split("\n  }", 1)[0]
    assert "codes.indexOf(state.selected) !== -1" in fn
    assert "return; // seçim hâlâ geçerli - DOKUNMA" in fn
