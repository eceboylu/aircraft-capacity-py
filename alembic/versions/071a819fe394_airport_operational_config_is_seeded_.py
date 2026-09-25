"""airport operational config is_seeded_default

Revision ID: 071a819fe394
Revises: fc0ecac68804
Create Date: 2026-09-25 03:27:47.186622

ADIM (Airport Operational Config Materialization) - `airport_
operational_configs`'a `is_seeded_default` (Boolean, NOT NULL) eklenir.
`server_default='0'` İLE eklenir - bu tablo production'da BOŞ olmayabilir
(elle eklenmiş override satırları olabilir, bkz. models.py docstring'i:
"elle INSERT edilen satırlar hep override sayılır") - `server_default`
OLMADAN bu ADD COLUMN, mevcut satırlar varken NOT NULL kısıtı yüzünden
BAŞARISIZ olurdu. Autogenerate'in ürettiği ham halinde bu YOKTU - elle
eklendi (bkz. rapor).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '071a819fe394'
down_revision: Union[str, Sequence[str], None] = 'fc0ecac68804'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'airport_operational_configs',
        sa.Column('is_seeded_default', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('airport_operational_configs', 'is_seeded_default')
