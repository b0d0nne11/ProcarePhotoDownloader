"""Model objects."""

import datetime as dt
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Self
from urllib.parse import urlparse

import sqlalchemy as sa
from pathvalidate import sanitize_filename
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql.dml import ReturningInsert
from sqlmodel import Field, Relationship, SQLModel, col, select
from sqlmodel.sql.expression import SelectOfScalar

DEFAULT_MAX_ATTEMPTS = 3


class MediaKind(StrEnum):
    """
    What an item is and by extension which listing it came from.

    ``PHOTO`` and ``VIDEO`` come from the gallery. ``ACTIVITY`` is a photo
    staff attached to a timeline entry, which the gallery never reports.
    """

    PHOTO = "photo"
    VIDEO = "video"
    ACTIVITY = "activity"


class AwareDateTime(sa.types.TypeDecorator):
    """
    A timestamp stored as ISO-8601 text including the offset.

    SQLAlchemy's ``DateTime`` would drop the ``tzinfo``, and ``.timestamp()``
    then falls back to the machine's zone, not the school's.
    """

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: object) -> str | None:
        return None if value is None else value.isoformat()

    def process_result_value(self, value: str | None, dialect: object) -> dt.datetime | None:
        return None if value is None else dt.datetime.fromisoformat(value)


class Kid(SQLModel, table=True):
    """A child."""

    __tablename__ = "kids"

    id: str = Field(primary_key=True)
    name: str = ""
    created_at: dt.datetime | None = Field(default=None, sa_type=AwareDateTime)

    @classmethod
    def upsert(cls) -> ReturningInsert[tuple[Self]]:
        """Insert or update children."""
        query = sqlite_insert(cls)
        query = query.on_conflict_do_update(
            index_elements=[cls.id],
            set_={
                "name": query.excluded.name,
                "created_at": query.excluded.created_at,
            },
        ).returning(cls)
        return query


class MediaItem(SQLModel, table=True):
    """
    A single photo or video belonging to a child.

    Keyed by ``(id, kid_id)`` rather than by ``id`` alone. Procare returns a
    copy of each photo for every child in it so each child gets their own copy
    in their own folder.

    ``url`` is a CloudFront pre-signed link. Procare issues them with a 90 day
    expiry, which is why the catalog can safely store them for later retries.

    The second set of columns tracks download state. ``local_path`` is relative
    to a common root directory so the media library is portable.
    """

    __tablename__ = "media"
    __table_args__ = (
        sa.Index("media_kid_captured", "kid_id", "captured_at"),
        sa.Index("media_pending", "downloaded_at", sqlite_where=sa.text("downloaded_at IS NULL")),
    )

    id: str = Field(primary_key=True)
    kid_id: str = Field(foreign_key="kids.id", primary_key=True)
    kind: MediaKind = Field(sa_column=sa.Column(sa.Enum(MediaKind), nullable=False))
    captured_at: dt.datetime = Field(sa_type=AwareDateTime)
    url: str
    caption: str = ""

    local_path: str | None = None
    size_bytes: int | None = None
    downloaded_at: dt.datetime | None = Field(default=None, sa_type=AwareDateTime)
    attempts: int = 0
    last_error: str | None = None

    kid: "Kid" = Relationship()

    @property
    def path(self) -> Path:
        """Where this item lives in the library, relative to the library's root.

        Format: ``<Child Name>/<year>/<YYYY-MM-DD>_<HHMMSS>_<id8>.<ext>``

        The layout is browsable without this tool. The timestamp prefix sorts
        chronologically in any file browser and the short id suffix keeps names
        unique when several photos share a second.
        """
        kid_name = sanitize_filename(self.kid.name).strip(" .") or "unknown"
        stamp = self.captured_at.strftime("%Y-%m-%d_%H%M%S")
        suffix = PurePosixPath(urlparse(self.url).path).suffix.lower()
        if not suffix[1:].isalnum():
            suffix = ".mp4" if self.kind == MediaKind.VIDEO else ".jpg"
        return Path(kid_name) / str(self.captured_at.year) / f"{stamp}_{self.id[:8]}{suffix}"

    @classmethod
    def upsert(cls) -> ReturningInsert[tuple[Self]]:
        """Insert or update media items.

        Only the signed URL and caption are overwritten during an update. Other
        fields related to the download state are maintained by the application.
        """
        query = sqlite_insert(cls)
        query = query.on_conflict_do_update(
            index_elements=[cls.id, cls.kid_id],
            set_={
                "url": query.excluded.url,
                "caption": query.excluded.caption,
            },
        ).returning(cls)
        return query

    def mark_downloaded(self, local_path: Path, size_bytes: int) -> sa.Update:
        """Record a finished download."""
        query = (
            sa.update(MediaItem)
            .where(
                col(MediaItem.id) == self.id,
                col(MediaItem.kid_id) == self.kid_id,
            ).values(
                local_path=str(local_path),
                size_bytes=size_bytes,
                downloaded_at=dt.datetime.now(dt.UTC),
                last_error=None,
            )
        )
        return query

    def mark_failed(self, error: str) -> sa.Update:
        """Count one failed attempt and record why."""
        query = (
            sa.update(MediaItem)
            .where(
                col(MediaItem.id) == self.id,
                col(MediaItem.kid_id) == self.kid_id,
            ).values(
                attempts=col(MediaItem.attempts) + 1,
                last_error=error,
            )
        )
        return query

    @classmethod
    def reset_attempts(cls) -> sa.Update:
        """Clear the failure counters."""
        query = (
            sa.update(cls)
            .where(
                col(cls.downloaded_at).is_(None),
                col(cls.attempts) > 0,
            ).values(
                attempts=0
            )
        )
        return query

    @classmethod
    def find_missing(
        cls,
        kid_id: str | None = None,
    ) -> SelectOfScalar[Self]:
        """Items the catalog knows about but has not yet stored on disk."""
        query = select(cls).where(
            col(cls.downloaded_at).is_(None),
        )
        if kid_id:
            query = query.where(col(cls.kid_id) == kid_id)
        query = query.order_by(col(cls.captured_at).desc())
        return query

    @classmethod
    def find_downloaded(cls) -> SelectOfScalar[Self]:
        """Items already synced to disk."""
        query = select(cls).where(
            col(cls.downloaded_at).is_not(None),
        )
        query = query.order_by(col(cls.captured_at).desc())
        return query

    @classmethod
    def iter_all(cls) -> SelectOfScalar[Self]:
        """Every item."""
        query = select(cls).order_by(col(cls.captured_at).desc())
        return query
