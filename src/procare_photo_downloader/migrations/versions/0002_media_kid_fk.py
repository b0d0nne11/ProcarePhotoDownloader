"""Give media a real foreign key onto the children.

Orphan media rows cannot survive the constraint and each one is a real
download worth keeping, so every ``kid_id`` with no matching child first gets
a placeholder kid named ``unknown``. Cli renders exactly those rows the same
way today, so the visible output of a migrated catalog is unchanged.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # One placeholder per ghost id, not one shared: media is keyed by
    # (id, kid_id) and the key must resolve for every row.
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO kids (id, name, created_at)
            SELECT DISTINCT m.kid_id, 'unknown', NULL
            FROM media m
            WHERE NOT EXISTS (SELECT 1 FROM kids k WHERE k.id = m.kid_id)
            """
        )
    )
    # SQLite cannot add a constraint in place, so this rebuilds the table.
    with op.batch_alter_table("media", schema=None) as batch:
        batch.create_foreign_key("fk_media_kid_id", "kids", ["kid_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("media", schema=None) as batch:
        batch.drop_constraint("fk_media_kid_id", type_="foreignkey")
    # The placeholder kids stay: picking them out again by name is guessing.
