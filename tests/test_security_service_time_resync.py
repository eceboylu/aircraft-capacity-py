"""
Security service time (en son: 1.5 -> 1.0 dk, ondan önce 0.4 -> 1.5 dk) -
her production behavior change'in DB'de zaten VAR OLAN `is_seeded_
default=True` satırlara da yansıması gerekiyor (yeni Python default'u
sadece YENİ satırları etkiler). `resync_security_service_time()` bu
satırları HANGİ eski değerde olursa olsun o ANKİ `SECURITY_EFFECTIVE_
SERVICE_TIME_MINUTES` constant'ına taşır; insan eliyle override edilmiş
(`is_seeded_default=False`) satırlara DOKUNMAZ.

NOT: bu test gerçek bir MySQL bağlantısı (`db_session` fixture,
DATABASE_URL bir `..._test` veritabanını göstermeli) gerektirir - bu
oturumda Docker/MySQL erişimi olmadığı için ÇALIŞTIRILAMADI, ileride
uygun bir ortamda çalıştırılmak üzere yazıldı.
"""
from app.queue.constants import SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES
from app.queue.models import Airport, AirportOperationalConfig
from app.queue.pipeline import ensure_airport_operational_configs, resync_security_service_time


def test_resync_updates_only_seeded_default_rows(db_session):
    session = db_session
    session.add_all([
        Airport(iata_code="IST", icao_code="LTFM", scale="mega"),
        Airport(iata_code="ESB", icao_code="LTAC", scale="large"),
    ])
    session.commit()

    ensure_airport_operational_configs(session)

    # Bir satırı, HEMEN BİR ÖNCEKİ contract değerine (1.5) manuel geri al
    # - "önceden seed edilmiş ama henüz resync edilmemiş" gerçek durumu
    # simüle eder. Mekanizma herhangi bir eski değerden çalışmalı (0.4'ten
    # de, 1.5'ten de) - sadece o ANKİ constant'ı hedef alır.
    seeded_row = session.get(AirportOperationalConfig, "IST")
    seeded_row.security_service_time_minutes = 1.5
    session.commit()

    # Diğerini insan eliyle override edilmiş gibi işaretle - resync
    # ONA DOKUNMAMALI.
    overridden_row = session.get(AirportOperationalConfig, "ESB")
    overridden_row.security_service_time_minutes = 0.6
    overridden_row.is_seeded_default = False
    session.commit()

    resynced_count = resync_security_service_time(session)
    assert resynced_count == 1

    session.expire_all()
    assert session.get(AirportOperationalConfig, "IST").security_service_time_minutes == SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES
    assert session.get(AirportOperationalConfig, "ESB").security_service_time_minutes == 0.6


def test_resync_is_idempotent(db_session):
    session = db_session
    session.add(Airport(iata_code="IST", icao_code="LTFM", scale="mega"))
    session.commit()
    ensure_airport_operational_configs(session)

    first = resync_security_service_time(session)
    second = resync_security_service_time(session)

    assert first == 0  # yeni satır zaten güncel constant ile seed edildi
    assert second == 0
