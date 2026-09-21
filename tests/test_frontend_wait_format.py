"""
Bu turun kullanıcı isteği (Frontend Wait-Time Formatting) doğrulaması.

`formatWaitMinutes()` SADECE gösterim katmanıdır - backend/API dakika
değerlerini DEĞİŞTİRMEZ (bkz. index.html'deki fonksiyon docstring'i).
Bu test dosyası iki katmanda doğrular:

  1) STATİK: fonksiyonun gerçekten `waitTimeText()` içinden merkezi
     olarak çağrıldığını, mantığın BAŞKA hiçbir yerde kopyalanmadığını
     kaynak metin üzerinden doğrular (mevcut `test_frontend_4_graph_
     contract.py` ile AYNI desen - pytest JS çalıştıramaz).
  2) GERÇEK ÇALIŞTIRMA: `formatWaitMinutes()`'ın kaynak kodunu
     `index.html`'den AYNEN çıkarıp Node.js ile gerçekten çalıştırır
     (bu makinede Node mevcut) - sadece string pattern eşleşmesi değil,
     GERÇEK dönüş değerleri doğrulanır. Node yoksa bu testler skip
     edilir (CI/local ortam farkı - suit'i kırmaz).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).parents[1] / "app" / "web" / "static" / "index.html"

NODE = shutil.which("node")


def _html():
    return INDEX_HTML.read_text(encoding="utf-8")


def _script():
    return _html().split("<script>", 1)[1].split("</script>", 1)[0]


def _extract_function(name: str) -> str:
    script = _script()
    marker = f"function {name}("
    start = script.index(marker)
    depth = 0
    i = script.index("{", start)
    body_start = i
    while True:
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return script[start:i + 1]


# ========================================================================
# STATİK - merkezi helper kullanımı, kopya mantık yok.
# ========================================================================

def test_format_wait_minutes_is_a_single_centralized_helper():
    script = _script()
    assert script.count("function formatWaitMinutes(") == 1


def test_wait_time_text_delegates_to_format_wait_minutes_not_raw_concat():
    fn = _extract_function("waitTimeText")
    assert "formatWaitMinutes(w.estimated_wait_minutes)" in fn
    # Eski ham birleştirme (" dk" doğrudan sayının arkasına eklenmesi) YOK.
    assert '+ " dk"' not in fn


def test_all_five_wait_surfaces_route_through_wait_time_text():
    """
    Overall / Domestic Security / International Departure — Passport /
    International Departure — Security / International Arrival - hepsi
    (artık BAĞIMSIZ panel/grafik olarak, bkz. api.py
    `_international_departure_split`) `waitDetailHtml()` üzerinden AYNI
    `waitTimeText()`'e gider (bkz. index.html) - dolayısıyla hepsi AYNI
    `formatWaitMinutes()`'ı kullanır, mantık beş yerde kopyalanmaz.
    """
    fn = _extract_function("waitDetailHtml")
    assert fn.count("waitTimeText(") == 1


def test_format_wait_minutes_never_duplicated_elsewhere_in_script():
    script = _script()
    # " saat" biçimlendirme mantığı SADECE formatWaitMinutes içinde.
    fn = _extract_function("formatWaitMinutes")
    remainder = script.replace(fn, "")
    assert '"  saat"' not in remainder
    assert "Math.floor(" not in remainder.replace(fn, "")


# ========================================================================
# GERÇEK ÇALIŞTIRMA (Node.js) - istenen tüm değerler.
# ========================================================================

CASES = [
    (0, "0 dk"),
    (15, "15 dk"),
    (30, "30 dk"),
    (59, "59 dk"),
    (60, "1 saat"),
    (61, "1 saat 1 dk"),
    (65, "1 saat 5 dk"),
    (90, "1 saat 30 dk"),
    (119, "1 saat 59 dk"),
    (120, "2 saat"),
    (125, "2 saat 5 dk"),
    (145, "2 saat 25 dk"),
    (180, "3 saat"),
    (None, "Hesaplanamıyor"),
]


@pytest.mark.skipif(NODE is None, reason="Node.js bu ortamda bulunamadı")
def test_format_wait_minutes_real_execution_matches_all_required_cases():
    fn_source = _extract_function("formatWaitMinutes")
    cases_json = json.dumps([c[0] for c in CASES])
    script = (
        fn_source
        + "\nconst inputs = " + cases_json + ";"
        + "\nconsole.log(JSON.stringify(inputs.map(formatWaitMinutes)));"
    )
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout.strip())
    expected = [c[1] for c in CASES]
    assert outputs == expected


# ========================================================================
# ADIM (Wait Risk-Independence) - wait, risk seviyesine göre
# GİZLENMEMELİ/ÜRETİLMEMELİ - ikisi ayrı kavramdır (bkz. rapor Bölüm 22).
# ========================================================================

def test_wait_time_text_never_references_risk():
    """
    `waitTimeText()` SADECE `estimated_wait_minutes`/`utilization`'a
    bakar - risk seviyesine göre wait'i gizleyen/gösteren bir dal YOK.
    """
    fn = _extract_function("waitTimeText")
    assert "risk" not in fn.lower()


def test_wait_detail_html_never_gates_on_risk():
    """`waitDetailHtml()` risk seviyesine bakmaksızın HER ZAMAN wait metnini üretir."""
    fn = _extract_function("waitDetailHtml")
    assert "risk" not in fn.lower()
    assert "HIGH" not in fn and "CRITICAL" not in fn and "LOW" not in fn


@pytest.mark.skipif(NODE is None, reason="Node.js bu ortamda bulunamadı")
def test_wait_time_text_shows_wait_for_zero_low_and_high_values_alike():
    """
    Bölüm 20: risk seviyesinden BAĞIMSIZ - 0/3/8/20/65 dakikalık
    HERHANGİ bir `estimated_wait_minutes` değeri, risk LOW/MEDIUM/HIGH/
    CRITICAL fark etmeksizin AYNI şekilde metne dönüşür (waitTimeText
    zaten risk'i hiç okumuyor - bkz. yukarıdaki statik test).
    """
    wait_fn = _extract_function("formatWaitMinutes")
    text_fn = _extract_function("waitTimeText")
    cases = [0, 3, 8, 20, 65]
    script = (
        wait_fn + "\n" + text_fn
        + "\nconst cases = " + json.dumps(cases) + ";"
        + "\nconsole.log(JSON.stringify(cases.map(function(m){"
        + "return waitTimeText({estimated_wait_minutes: m, utilization: 0.5});"
        + "})));"
    )
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout.strip())
    assert outputs == ["0 dk", "3 dk", "8 dk", "20 dk", "1 saat 5 dk"]


@pytest.mark.skipif(NODE is None, reason="Node.js bu ortamda bulunamadı")
def test_format_wait_minutes_does_not_append_zero_minutes_to_exact_hours():
    fn_source = _extract_function("formatWaitMinutes")
    script = (
        fn_source
        + "\nconsole.log(JSON.stringify([formatWaitMinutes(60), formatWaitMinutes(120)]));"
    )
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout.strip())
    assert outputs == ["1 saat", "2 saat"]
    assert "saat 0 dk" not in json.dumps(outputs)
