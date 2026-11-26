import datetime as dt
from dataclasses import fields
from operator import attrgetter
from unittest.mock import Mock, call, patch

import httpx
import pytest

from procare_photo_downloader.db import session_scope
from procare_photo_downloader.models import DEFAULT_MAX_ATTEMPTS, MediaItem, MediaKind
from procare_photo_downloader.sync import EARLIEST_SUPPORTED, Syncer, SyncHooks, SyncStats
from tests.factories import MediaItemFactory


@pytest.fixture
def output_dir(tmp_path):
    return tmp_path / "library"


@pytest.fixture
def session(output_dir, kid):
    with session_scope(output_dir) as session:
        session.add(kid)
        session.commit()
        yield session


@pytest.fixture
def hooks():
    return SyncHooks(**{entry.name: Mock() for entry in fields(SyncHooks)})


@pytest.fixture
def content():
    return b"x" * 100


@pytest.fixture
def client(mock_client, content):
    return mock_client(lambda _request: httpx.Response(200, content=content))


@pytest.fixture
def syncer(client, session, output_dir, hooks):
    return Syncer(client, session, output_dir, hooks)


class TestSyncStats:

    def test_merge(self):
        stats = SyncStats(discovered=1, downloaded=2, bytes_written=3)
        stats.merge(SyncStats(discovered=10, adopted=20, bytes_written=30))
        assert stats == SyncStats(discovered=11, downloaded=2, adopted=20, bytes_written=33)


class TestSyncer:

    def test_discover(self, syncer, session, kid):
        media_items = [
            *MediaItemFactory.build_batch(2, kid=kid, kind=MediaKind.PHOTO),
            *MediaItemFactory.build_batch(1, kid=kid, kind=MediaKind.VIDEO),
        ]
        since = dt.date(2026, 1, 1)
        until = dt.date(2026, 1, 31)
        def fake_iter_media(_kid, kind, *_args, **_kwargs):
            return (item for item in media_items if item.kind == kind)
        with patch.object(syncer.client, "iter_media", side_effect=fake_iter_media) as iter_media:
            stats = syncer.discover([kid], [MediaKind.PHOTO, MediaKind.VIDEO], since, until)
        assert stats == SyncStats(discovered=3)
        assert iter_media.call_args_list == [
            call(kid, MediaKind.PHOTO, since, until, on_window=syncer.hooks.on_window),
            call(kid, MediaKind.VIDEO, since, until, on_window=syncer.hooks.on_window),
        ]
        assert syncer.hooks.on_discovered.call_args_list == [
            call(kid, MediaKind.PHOTO, 2),
            call(kid, MediaKind.VIDEO, 1),
        ]
        catalog = list(session.exec(MediaItem.iter_all()))
        assert sorted(catalog, key=attrgetter("id")) == sorted(media_items, key=attrgetter("id"))

    @pytest.mark.freeze_time("2026-03-01")
    def test_discover_default_range(self, syncer, kid):
        since = kid.created_at.date()
        until = dt.date(2026, 3, 1)
        with patch.object(syncer.client, "iter_media", return_value=iter([])) as iter_media:
            stats = syncer.discover([kid], [MediaKind.PHOTO], None, None)
        assert stats == SyncStats()
        assert iter_media.call_args_list == [
            call(kid, MediaKind.PHOTO, since, until, on_window=syncer.hooks.on_window)
        ]

    @pytest.mark.freeze_time("2026-03-01")
    def test_discover_default_range_without_created_at(self, syncer, kid):
        since = EARLIEST_SUPPORTED
        until = dt.date(2026, 3, 1)
        kid.created_at = None
        with patch.object(syncer.client, "iter_media", return_value=iter([])) as iter_media:
            stats = syncer.discover([kid], [MediaKind.PHOTO], None, None)
        assert stats == SyncStats()
        assert iter_media.call_args_list == [
            call(kid, MediaKind.PHOTO, since, until, on_window=syncer.hooks.on_window)
        ]

    def test_discover_already_present(self, syncer, session, kid):
        media_items = MediaItemFactory.build_batch(2, kid=kid)
        session.add_all(media_items)
        session.commit()
        session.exec(media_items[0].mark_downloaded(media_items[0].path, 100))
        session.commit()
        def fake_iter_media(_kid, _kind, *_args, **_kwargs):
            yield from [
                item.model_copy(update={"url": f"{item.url}?sig=new"}) for item in media_items
            ]
        with patch.object(syncer.client, "iter_media", side_effect=fake_iter_media):
            stats = syncer.discover([kid], [MediaKind.PHOTO], None, None)
        assert stats == SyncStats(discovered=1, already_present=1)
        catalog = list(session.exec(MediaItem.iter_all()))
        assert catalog and all(item.url.endswith("?sig=new") for item in catalog)

    def test_discover_excludes_attempted(self, syncer, session, kid):
        media_items = MediaItemFactory.build_batch(3, kid=kid)
        media_items[0].attempts = 1
        media_items[1].attempts = DEFAULT_MAX_ATTEMPTS
        session.add_all(media_items)
        session.commit()
        with patch.object(syncer.client, "iter_media", return_value=iter(media_items)):
            stats = syncer.discover([kid], [MediaKind.PHOTO], None, None)
        assert stats == SyncStats(discovered=1)
        assert syncer.hooks.on_discovered.call_args_list == [
            call(kid, MediaKind.PHOTO, 3),
        ]

    def test_discover_skips_missing_url(self, syncer, session, kid):
        media_items = [
            MediaItemFactory.build(kid=kid),
            MediaItemFactory.build(kid=kid, url=""),
        ]
        with patch.object(syncer.client, "iter_media", return_value=iter(media_items)):
            stats = syncer.discover([kid], [MediaKind.PHOTO], None, None)
        assert stats == SyncStats(discovered=1)
        assert syncer.hooks.on_discovered.call_args_list == [
            call(kid, MediaKind.PHOTO, 1),
        ]
        catalog = list(session.exec(MediaItem.iter_all()))
        assert catalog == media_items[:1]

    def test_discover_deduplicates(self, syncer, session, kid):
        media_items = MediaItemFactory.build_batch(1, kid=kid) * 2
        with patch.object(syncer.client, "iter_media", return_value=iter(media_items)):
            stats = syncer.discover([kid], [MediaKind.PHOTO], None, None)
        assert stats == SyncStats(discovered=1)
        assert syncer.hooks.on_discovered.call_args_list == [
            call(kid, MediaKind.PHOTO, 1),
        ]
        catalog = list(session.exec(MediaItem.iter_all()))
        assert catalog == media_items[:1]

    @pytest.mark.freeze_time()
    def test_download_pending(
        self, syncer, session, hooks, output_dir, kid, content, sent_requests
    ):
        media_items = MediaItemFactory.build_batch(2, kid=kid)
        session.add_all(media_items)
        session.commit()
        stats = syncer.download_pending([kid])
        assert stats == SyncStats(downloaded=2, bytes_written=2 * len(content))
        sent_urls = set(str(request.url) for request in sent_requests)
        for item in media_items:
            assert (output_dir / item.path).read_bytes() == content
            assert item.url in sent_urls
        assert hooks.on_download_start.call_args_list == [call(2)]
        assert hooks.on_download_done.call_args_list == [call(), call()]
        assert hooks.on_download_failed.call_args_list == []
        catalog = list(session.exec(MediaItem.iter_all()))
        assert [
            (item.local_path, item.size_bytes, item.downloaded_at)
            for item in sorted(catalog, key=attrgetter("id"))
        ] == [
            (str(item.path), len(content), dt.datetime.now(tz=dt.UTC))
            for item in sorted(media_items, key=attrgetter("id"))
        ]

    @pytest.mark.freeze_time()
    def test_download_pending_adopts_existing_files(
        self, syncer, session, hooks, output_dir, kid, sent_requests
    ):
        media_items = MediaItemFactory.build_batch(1, kid=kid)
        session.add_all(media_items)
        session.commit()
        existing_path = output_dir / media_items[0].path
        existing_path.parent.mkdir(parents=True)
        existing_path.write_bytes(b"y" * 42)
        stats = syncer.download_pending([kid])
        assert stats == SyncStats(adopted=1)
        assert sent_requests == []
        assert hooks.on_download_start.call_args_list == []
        catalog = list(session.exec(MediaItem.iter_all()))
        assert [
            (item.local_path, item.size_bytes, item.downloaded_at)
            for item in sorted(catalog, key=attrgetter("id"))
        ] == [
            (str(item.path), 42, dt.datetime.now(tz=dt.UTC))
            for item in sorted(media_items, key=attrgetter("id"))
        ]

    def test_download_pending_skips_exhausted(
        self, syncer, session, hooks, output_dir, kid, content, sent_requests
    ):
        media_items = [
            MediaItemFactory.build(kid=kid),
            MediaItemFactory.build(kid=kid, attempts=DEFAULT_MAX_ATTEMPTS),
        ]
        session.add_all(media_items)
        session.commit()
        stats = syncer.download_pending([kid])
        assert stats == SyncStats(
            downloaded=1, skipped_unavailable=1, bytes_written=len(content)
        )
        sent_urls = set(str(request.url) for request in sent_requests)
        assert sent_urls == {media_items[0].url}
        assert hooks.on_download_start.call_args_list == [call(1)]
        assert not (output_dir / media_items[1].path).exists()

    def test_download_pending_failed(
        self, session, hooks, output_dir, kid, content, mock_client, sent_requests
    ):
        media_items = MediaItemFactory.build_batch(2, kid=kid)
        session.add_all(media_items)
        session.commit()
        def handler(request):
            if str(request.url) == media_items[0].url:
                return httpx.Response(404)
            return httpx.Response(200, content=content)
        syncer = Syncer(mock_client(handler), session, output_dir, hooks)
        stats = syncer.download_pending([kid])
        assert stats == SyncStats(downloaded=1, failed=1, bytes_written=len(content))
        assert (output_dir / media_items[1].path).read_bytes() == content
        sent_urls = set(str(request.url) for request in sent_requests)
        for item in media_items:
            assert item.url in sent_urls
        assert hooks.on_download_start.call_args_list == [call(2)]
        assert hooks.on_download_done.call_args_list == [call(), call()]
        failed_item = session.get(MediaItem, (media_items[0].id, kid.id))
        assert failed_item.attempts == 1
        assert failed_item.downloaded_at is None
        assert failed_item.last_error.startswith("Client error '404 Not Found'")
        assert hooks.on_download_failed.call_args_list == [
            call(failed_item, failed_item.last_error)
        ]
        assert not (output_dir / failed_item.path).exists()


    def test_download_pending_nothing_to_do(self, syncer, hooks, kid, sent_requests):
        assert syncer.download_pending([kid]) == SyncStats()
        assert sent_requests == []
        assert hooks.on_download_start.call_args_list == []

    def test_run(self, syncer, session, output_dir, kid, content, sent_requests):
        media_items = MediaItemFactory.build_batch(2, kid=kid)
        with patch.object(syncer.client, "iter_media", return_value=iter(media_items)):
            stats = syncer.run([kid], [MediaKind.PHOTO], None, None)
        assert stats == SyncStats(discovered=2, downloaded=2, bytes_written=2 * len(content))
        sent_urls = set(str(request.url) for request in sent_requests)
        for item in media_items:
            assert (output_dir / item.path).read_bytes() == content
            assert item.url in sent_urls

    def test_run_dry_run(self, syncer, session, kid, sent_requests):
        media_items = MediaItemFactory.build_batch(2, kid=kid)
        with patch.object(syncer.client, "iter_media", return_value=iter(media_items)):
            stats = syncer.run([kid], [MediaKind.PHOTO], None, None, dry_run=True)
        assert stats == SyncStats(discovered=2)
        assert sent_requests == []
        catalog = list(session.exec(MediaItem.iter_all()))
        assert all(item.downloaded_at is None for item in catalog)
