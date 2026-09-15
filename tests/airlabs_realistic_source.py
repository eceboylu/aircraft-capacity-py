"""
`tests/fixtures/airlabs_realistic/` fixture'ını GERÇEK
`airlabs_client`/`pipeline` zincirine besleyen mock kaynak.

`tests/airlabs_mock_source.py` (ADIM 4) ve `tests/airlabs_operational_source.py`
(ADIM 6A) ile AYNI desen: sadece `urllib.request.urlopen` sahtelenir,
gerçek client/pagination/retry kodu HİÇ değişmeden çalışır.

Sadece TEK round (`t0`) var - bu fixture bir T0/T1/T2 senaryosu değil,
sadece daha fazla havalimanı için gerçekçi HACİM sağlıyor.
"""

from __future__ import annotations

import glob
import json
import os
import urllib.parse
import urllib.request

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "airlabs_realistic")

# `generate.py`'deki TARGET_AIRPORTS ile AYNI liste - `data/Delays -
# Type *.json`'daki (gerçek örnek veri) TÜM havalimanları, IST/SAW/ADB
# hariç (bkz. generate.py'nin kendi listesi/yorumu).
AIRPORTS = [
    "AEP", "AER", "ALC", "AMS", "ASR", "AYT", "CAI", "CAN", "CBR", "CDG",
    "CGK", "CGO", "CHQ", "CKG", "CUN", "DAD", "DAT", "DEL", "DEN", "DIG",
    "DIN", "DLC", "DME", "DPS", "DUB", "DXB", "FBM", "FOC", "FUK", "HAN",
    "HGH", "HND", "HNL", "HRB", "ICN", "IXB", "IXJ", "JED", "KBL", "KMJ",
    "KOJ", "KRL", "KZN", "LAX", "LTN", "MEL", "MUC", "OAG", "OKA", "ORN",
    "ORY", "PHE", "PKX", "PUS", "RAK", "RTM", "RUH", "SEA", "SJU", "SKG",
    "SQD", "SUB", "SVO", "SVQ", "SVX", "SZX", "TAE", "TAG", "TAO", "TFN",
    "TFU", "TPE", "VIE", "WEH", "WUH", "YTY", "ZRH",
]


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _page_files(airport: str, direction: str) -> list[str]:
    pattern = os.path.join(FIXTURES_ROOT, "t0", f"schedules_{airport}_{direction}_page_*.json")
    files = sorted(glob.glob(pattern), key=lambda p: int(p.rsplit("_page_", 1)[1].split(".")[0]))
    if not files:
        raise FileNotFoundError(f"realistic fixture bulunamadı: {pattern}")
    return files


def _urlopen(request, timeout=None) -> _FakeHTTPResponse:
    parsed = urllib.parse.urlparse(request.full_url)
    query = urllib.parse.parse_qs(parsed.query)
    endpoint = parsed.path.rsplit("/", 1)[-1]

    if endpoint == "flights":
        return _FakeHTTPResponse({
            "_mock_disclaimer": "SYNTHETIC - bu fixture'da Kaynak B üretilmedi, aircraft_icao Kaynak A'nın kendi alanından geliyor.",
            "request": {"host": "airlabs.co", "method": "flights", "has_more": False},
            "response": [],
        })

    if "dep_iata" in query:
        airport, direction = query["dep_iata"][0], "departure"
    else:
        airport, direction = query["arr_iata"][0], "arrival"

    files = _page_files(airport, direction)
    offset = int(query.get("offset", ["0"])[0])

    cumulative = 0
    chosen_path = files[-1]
    for path in files:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        page_size = len(payload["response"])
        if offset < cumulative + page_size or not payload["request"]["has_more"]:
            chosen_path = path
            break
        cumulative += page_size

    with open(chosen_path, encoding="utf-8") as fh:
        return _FakeHTTPResponse(json.load(fh))


def install(monkeypatch) -> None:
    """`urllib.request.urlopen`'ı bu mock kaynağa bağlar. Gerçek ağa HİÇ çıkılmaz."""
    monkeypatch.setenv("AIRLABS_API_KEY", "test-dummy-key-not-real-never-sent")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
