"""
ADIM (Retention Domain Timestamps) - Flight/FlightEvent/QueuePrediction
retention ve 48-saatlik usage-horizon kararları için TEK canonical zaman
kuralı.

SAF KATMAN: veritabanına/dosyaya dokunmaz - `domain/operational_day.py`
ile AYNI desende, sadece saf fonksiyonlar.

Flight için canonical zaman `build_flight_key()` (ingestion/sources.py)
İLE AYNI kuralı kullanır: departure -> dep_scheduled_utc, arrival ->
arr_scheduled_utc (actual/estimated DEĞİL - sadece scheduled). Bu,
flight_key'in zaten kullandığı kimlik zamanıyla TUTARLI kalır - bir
flight gecikse/erken kalksa bile retention kararı DEĞİŞMEZ (flight_key
de değişmiyor, aynı satır güncellenmeye devam ediyor).

Bu modül ÜÇ ayrı çağıran tarafından kullanılır (aynı mantık HİÇBİR
YERDE kopyalanmaz):
  1) `engine.py:flights_of_airport()` - 48h usage-horizon filtresi.
  2) `retention.py` - cleanup candidate hesaplama.
  3) `ingestion/sources.py` - re-ingest loop önleyici horizon reddi.
"""

from __future__ import annotations

from datetime import datetime

from ..constants import DIRECTION_DEPARTURE

# Bölüm 7/8 - SABİT "usage horizon" (48 saat) - configurable retention
# cleanup cadence'i (FLIGHT_RETENTION_DAYS vb., app/queue/retention.py)
# İLE KARIŞTIRILMAZ. Bu sabit, HEM `engine.py:flights_of_airport()`'un
# 48h usage-horizon filtresi HEM DE ingestion'ın re-ingest-loop
# önleyici horizon reddi (`pipeline.py:load_flight_rows()`) tarafından
# kullanılır - iki ayrı yerde iki farklı sayı YAZILMAZ.
USAGE_HORIZON_HOURS = 48


def canonical_operational_time(
    direction: str,
    dep_scheduled_utc: datetime | None,
    arr_scheduled_utc: datetime | None,
) -> datetime | None:
    """
    Bir flight'ın "operasyonel" zamanı - retention/usage-horizon kararları
    için TEK referans. `build_flight_key()`'in `operational_scheduled_utc`
    seçimiyle BİREBİR AYNI kural: departure -> dep_scheduled_utc,
    arrival -> arr_scheduled_utc.

    Scheduled zaman yoksa (None) - UYDURMA bir "eski" varsayımı
    ÜRETİLMEZ, None döner; çağıran taraf bunu "zaman bilinmiyor, silme/
    dışlama YAPMA" olarak ele almalı (bkz. çağıranlar - hepsi None'ı
    güvenli tarafta, yani KORUMA yönünde yorumluyor).
    """
    if direction == DIRECTION_DEPARTURE:
        return dep_scheduled_utc
    return arr_scheduled_utc


def canonical_flight_time(flight) -> datetime | None:
    """`canonical_operational_time()`'ın bir ORM `Flight` nesnesi üzerinden ince sarmalayıcısı."""
    return canonical_operational_time(
        flight.direction, flight.dep_scheduled_utc, flight.arr_scheduled_utc
    )
