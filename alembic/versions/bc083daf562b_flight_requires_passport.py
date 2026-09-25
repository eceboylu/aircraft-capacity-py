"""flight requires_passport (Schengen-aware routing)

Revision ID: bc083daf562b
Revises: c57ddf30bbc6
Create Date: 2026-09-25 18:00:00.000000

ADIM (Schengen-Aware Passport Routing) - `flights.requires_passport`
(Boolean, NOT NULL, default True) - `Flight.location` (traffic type:
domestic/international) İLE KARIŞTIRILMAMALI: bu KENDİ BAŞINA ayrı bir
sınır-kontrolü sinyalidir (bkz. `app/queue/domain/schengen.py`).
Varsayılan `True`, mevcut satırlar için ("Schengen bilgisi HENÜZ
hesaplanmadı") GÜVENLİ TARAF olan "passport gerekir" davranışını korur -
gerçek değer bir sonraki ingestion turunda `parse_source_a_record()`
tarafından yeniden hesaplanıp yazılır.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bc083daf562b'
down_revision: Union[str, Sequence[str], None] = 'c57ddf30bbc6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'flights',
        sa.Column(
            'requires_passport', sa.Boolean(), nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('flights', 'requires_passport')
