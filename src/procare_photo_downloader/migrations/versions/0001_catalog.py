"""
The catalog.

Squashed baseline. The catalog is a local cache of Procare's listings plus
download state, so it is rebuildable and there were no other installations to
carry forward. The column types are taken from the models rather than written
by hand, which is what keeps ``--autogenerate`` quiet.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

from procare_photo_downloader.models import AwareDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "kids",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", AwareDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "media",
        # Procare returns the same photo id for every child in it, and each
        # child gets their own copy in their own folder, so a group photo is
        # legitimately several rows.
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kid_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kind", sa.Enum("PHOTO", "VIDEO", "ACTIVITY", name="mediakind"), nullable=False),
        sa.Column("captured_at", AwareDateTime(), nullable=False),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("caption", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("local_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("downloaded_at", AwareDateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint("id", "kid_id"),
    )
    with op.batch_alter_table("media", schema=None) as batch:
        batch.create_index("media_kid_captured", ["kid_id", "captured_at"], unique=False)
        # Plain ascending. SQLite reads an index backwards just as happily, and
        # a DESC expression does not survive reflection, which leaves every
        # autogenerate wanting to rebuild it.
        batch.create_index(
            "media_pending",
            ["downloaded_at"],
            unique=False,
            sqlite_where=sa.text("downloaded_at IS NULL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("media", schema=None) as batch:
        batch.drop_index("media_pending", sqlite_where=sa.text("downloaded_at IS NULL"))
        batch.drop_index("media_kid_captured")
    op.drop_table("media")
    op.drop_table("kids")
