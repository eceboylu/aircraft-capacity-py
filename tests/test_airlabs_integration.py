"""
AirLabs GERÇEK API smoke test'i.

`AIRLABS_API_KEY` ortam değişkeni TANIMLI DEĞİLSE bu dosyadaki testler
OTOMATİK ATLANIR - normal `pytest` çalıştırması (CI dahil) bundan hiç
etkilenmez. Key varsa, TEK küçük bir gerçek çağrı yapılıp dönen ham
kaydın mevcut `parse_source_a_record()`'tan sorunsuz geçtiği doğrulanır.

Bu dosya kasıtlı olarak minimaldir: amaç kapsamlı bir entegrasyon
paketi değil, "gerçek API'ye bağlantı çalışıyor mu" kontrolüdür.
"""

import os

import pytest

from app.queue.ingestion import airlabs_client
from app.queue.ingestion.sources import parse_source_a_record

requires_real_key = pytest.mark.skipif(
    not os.environ.get("AIRLABS_API_KEY"),
    reason="AIRLABS_API_KEY tanımlı değil - gerçek API smoke test atlandı",
)

# Küçük, düşük trafikli bir havalimanı - gereksiz yere büyük bir
# response çekmemek için.
SMOKE_TEST_AIRPORT = "ESB"


@requires_real_key
def test_fetch_schedules_returns_parseable_records():
    records = airlabs_client.fetch_schedules("departure", SMOKE_TEST_AIRPORT)

    assert isinstance(records, list)
    if not records:
        pytest.skip(
            f"{SMOKE_TEST_AIRPORT} için şu anda planlı uçuş dönmedi - "
            "API bağlantısı çalışıyor ama örnek veri yok."
        )

    row = parse_source_a_record(records[0], "departure", {})
    # Kayıt tamamen atlanmadıysa (dep_iata boş değilse) temel alanlar dolu olmalı.
    if row is not None:
        assert row["airport_iata"]
        assert row["direction"] == "departure"


@requires_real_key
def test_fetch_live_flights_returns_list():
    records = airlabs_client.fetch_live_flights()
    assert isinstance(records, list)
