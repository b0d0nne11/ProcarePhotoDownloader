"""Orchestration: discover media, then fetch whatever is missing.

Sync runs in two phases rather than one streaming pipeline. Procare signs its
CDN URLs with a 90 day expiry, so there is no need to download a window before
listing the next one, and separating the phases means a download failure never
costs us the listing work already paid for in API calls.
"""

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
from functools import partial
from pathlib import Path
from typing import Self

from sqlmodel import Session as SQLModelSession

from .api import ProcareClient
from .errors import DownloadError, ProcareError
from .models import DEFAULT_MAX_ATTEMPTS, Kid, MediaItem, MediaKind
from .storage import download

log = logging.getLogger(__name__)

EARLIEST_SUPPORTED = dt.date(2015, 1, 1)


@dataclass(slots=True)
class SyncStats:
    """
    Counts from one sync run, as reported in the summary table.

    ``discovered`` and ``already_present`` come from the listing phase based on
    whether the catalog already records it. The rest come from the download
    phase. ``adopted`` is an item whose file was already in place, left by an
    earlier run or a rebuilt catalog.
    """

    discovered: int = 0
    already_present: int = 0
    downloaded: int = 0
    adopted: int = 0
    failed: int = 0
    skipped_unavailable: int = 0
    bytes_written: int = 0

    def merge(self, other: Self) -> None:
        """Merge with another set of stats."""
        for entry in fields(self):
            setattr(self, entry.name, getattr(self, entry.name) + getattr(other, entry.name))


@dataclass(slots=True)
class SyncHooks:
    """Presentation callbacks so this module stays free of any UI concern."""

    on_window: Callable[[Kid, MediaKind, dt.date, dt.date, int], None]
    on_discovered: Callable[[Kid, MediaKind, int], None]
    on_download_start: Callable[[int], None]
    on_download_done: Callable[[], None]
    on_download_failed: Callable[[MediaItem, str], None]


class Syncer:
    """
    Discover media through the API, then download whatever is missing.

    The catalog session is used only from the calling thread. Downloads run on a
    thread pool but their results are written to the catalog back on the calling
    thread as each one completes.
    """

    def __init__(
        self,
        client: ProcareClient,
        session: SQLModelSession,
        output_dir: Path,
        hooks: SyncHooks,
        concurrency: int = 8,
    ) -> None:
        self.client = client
        self.session = session
        self.output_dir = output_dir
        self.concurrency = max(1, concurrency)
        self.hooks = hooks

    def discover(
        self,
        kids: Sequence[Kid],
        kinds: Sequence[MediaKind],
        since: dt.date | None,
        until: dt.date | None,
    ) -> SyncStats:
        """
        Walk the API and record every item found in the catalog.

        Each kind is one listing, swept per child. Where the sweep stops is a
        per child question: a child's media hangs off their record, so none of
        it predates ``created_at``. Siblings enrol years apart, which is why an
        explicit ``since`` only ever raises that floor and never lowers it.

        The server's window filter does not cleanly partition items near UTC
        midnight boundaries (edges go out in UTC, items come back with local
        offsets), so the same item can be returned by two adjacent windows. The
        catalog is keyed and therefore immune, but the reported counts are not,
        which is why ``seen`` dedupes by the composite key.
        """
        stats = SyncStats()
        seen: set[tuple[str, str]] = set()
        since = since or EARLIEST_SUPPORTED
        until = until or dt.date.today()
        for kid in kids:
            for kind in kinds:
                found = 0
                for item in self.client.iter_media(
                    kid,
                    kind,
                    max(since, kid.created_at.date()) if kid.created_at else since,
                    until,
                    on_window=self.hooks.on_window,
                ):
                    if not item.url:
                        log.warning("skipping %s %s with no media URL", kind, item.id)
                        continue
                    if (item.id, item.kid_id) in seen:
                        continue
                    seen.add((item.id, item.kid_id))
                    found += 1
                    # RETURNING hands back any instance already in the identity
                    # map as is; populate_existing refreshes it from the row.
                    stored = self.session.exec(
                        MediaItem.upsert().execution_options(populate_existing=True),
                        params=item.model_dump(),
                    ).scalar_one()
                    if stored.downloaded_at is None:
                        stats.discovered += 1
                    else:
                        stats.already_present += 1
                self.session.commit()
                self.hooks.on_discovered(kid, kind, found)
        return stats

    def download_pending(
        self,
        kids: Sequence[Kid],
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> SyncStats:
        """Fetch everything the catalog has not yet stored on disk."""
        queue: list[MediaItem] = []
        stats = SyncStats()
        for kid in kids:
            for item in self.session.exec(MediaItem.find_missing(kid.id)):
                dest = self.output_dir / item.path
                if dest.is_file() and (size := dest.stat().st_size) > 0:
                    self.session.exec(item.mark_downloaded(item.path, size))
                    stats.adopted += 1
                    continue
                if item.attempts >= max_attempts:
                    stats.skipped_unavailable += 1
                    continue
                queue.append(item)
        if stats.adopted:
            self.session.commit()

        if not queue:
            return stats

        self.hooks.on_download_start(len(queue))
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {}
            for item in queue:
                future = pool.submit(
                    download,
                    partial(self.client.stream_media, item.url),
                    self.output_dir / item.path,
                    item.captured_at,
                )
                futures[future] = item
            for future in as_completed(futures):
                item = futures[future]
                try:
                    size = future.result()
                except (DownloadError, ProcareError) as e:
                    self.session.exec(item.mark_failed(str(e)))
                    self.hooks.on_download_failed(item, str(e))
                    stats.failed += 1
                    self.session.commit()
                else:
                    self.session.exec(item.mark_downloaded(item.path, size))
                    stats.downloaded += 1
                    stats.bytes_written += size
                self.hooks.on_download_done()
            self.session.commit()
        return stats

    def run(
        self,
        kids: Sequence[Kid],
        kinds: Sequence[MediaKind],
        since: dt.date | None,
        until: dt.date | None,
        dry_run: bool = False,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> SyncStats:
        """Run both phases and return their combined stats."""
        stats = self.discover(kids, kinds, since, until)
        if not dry_run:
            stats.merge(self.download_pending(kids, max_attempts=max_attempts))
        return stats
