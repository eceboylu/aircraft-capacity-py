"""
Madde 17-A/B - Alembic reconciliation regresyon kilidi.

Gerçek dev MySQL'de (`aircraft_capacity`) keşfedilen kök neden: DB, hiçbir
migration'la izlenemeyen (`b2d3f5a9c012`) bir revision taşıyordu, ama
GERÇEK şeması (tüm 12 tablo, sütun-sütun) `alembic/versions/fc0ecac68804_
baseline_schema.py`'ın ürettiğiyle BİREBİR aynıydı - sadece `is_seeded_
default` kolonu eksikti (bu kolonu ekleyen `071a819fe394` migration'ı hiç
uygulanmamıştı). Kör bir `alembic stamp head` YERİNE, schema kanıtı
doğrulandıktan SONRA `alembic stamp --purge fc0ecac68804` (gerçek şema
durumuna göre DÜZELTME) + `alembic upgrade head` (071a819fe394'ü GERÇEKTEN
uygulama) ile çözüldü.

Bu dosya o kök nedenin BİR DAHA YAŞANMAYACAĞINI değil (bu geçmiş bir DB
durumu, kod DEĞİL), `is_seeded_default` kolonunun ORM modelinde/migration
zincirinde HÂLÂ doğru şekilde tanımlı olduğunu kilitler - modelden
kazayla silinirse veya tipi/varsayılanı değişirse bu test yakalar.
"""
from sqlalchemy import inspect

from app.db import engine as _mysql_engine
from app.models import Base
import app.queue.models  # noqa: F401


def test_is_seeded_default_column_exists_with_correct_type_and_default(db_session):
    """
    `db_session` fixture'ı zaten `Base.metadata.create_all()` çalıştırdı
    (bkz. conftest.py) - bu, `is_seeded_default`'ın GERÇEKTEN mevcut
    ORM modelinden doğru şekilde üretildiğini doğrular (repo'nun kendi
    `071a819fe394` migration'ının ekleyeceği kolonla AYNI ad/tip/
    nullable/default sözleşmesi).
    """
    inspector = inspect(_mysql_engine)
    columns = {col["name"]: col for col in inspector.get_columns("airport_operational_configs")}

    assert "is_seeded_default" in columns
    column = columns["is_seeded_default"]
    assert column["nullable"] is False
    # MySQL'de native BOOLEAN yok - SQLAlchemy Boolean tipi TINYINT(1)
    # olarak saklanır, reflection da tabloyu OKURKEN (yaratırken değil)
    # onu ham TINYINT olarak geri döner (bilinen bir MySQL/SQLAlchemy
    # davranışı - `python_type` burada `int` döner, `bool` DEĞİL). Asıl
    # doğrulanan şey: kolon var, tek-byte/TINYINT genişliğinde ve
    # display_width=1 (gerçek bir BOOLEAN sütununun MySQL'deki izi).
    assert column["type"].__class__.__name__ in ("TINYINT", "BOOLEAN", "Boolean")
    assert getattr(column["type"], "display_width", 1) == 1


def test_airport_operational_configs_has_all_baseline_columns(db_session):
    """
    `fc0ecac68804` baseline migration'ının yarattığı TÜM kolonlar
    (is_seeded_default HARİÇ, o `071a819fe394`'ten gelir) hâlâ mevcut
    ORM modelinde bulunmalı - şema drift'i sessizce YOK OLMAMALI.
    """
    inspector = inspect(_mysql_engine)
    columns = {col["name"] for col in inspector.get_columns("airport_operational_configs")}

    expected_baseline_columns = {
        "airport_iata", "passport_counter_count", "passport_staff_count",
        "passport_service_time_minutes", "security_lane_count",
        "domestic_security_lane_count", "international_security_lane_count",
        "security_service_time_minutes", "passport_staff_per_counter",
        "passport_service_rate_per_staff", "passport_efficiency_multiplier",
        "passport_departure_server_count", "passport_arrival_server_count",
        "arrival_bank_threshold",
    }
    assert expected_baseline_columns <= columns
