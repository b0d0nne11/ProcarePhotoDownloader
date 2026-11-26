import datetime as dt
import re
from functools import partial
from unittest.mock import patch

import httpx
import pytest

from procare_photo_downloader.errors import DownloadError
from procare_photo_downloader.storage import download
from tests.factories import MediaItemFactory


class TestDownload:

    def test_download(self, tmp_path, mock_client, sent_requests):
        item = MediaItemFactory.build()
        content = b"x" * 100
        client = mock_client(lambda _request: httpx.Response(200, content=content))
        destination = tmp_path / item.path
        written = download(
            partial(client.stream_media, item.url),
            destination,
            item.captured_at,
        )
        assert [str(request.url) for request in sent_requests] == [item.url]
        assert written == len(content)
        assert destination.read_bytes() == content
        assert destination.stat().st_mtime == item.captured_at.timestamp()
        assert list(destination.parent.iterdir()) == [destination]

    @pytest.mark.freeze_time("2026-01-01T00:00:00Z")
    def test_download_expired(self, tmp_path, mock_client):
        expires = int(dt.datetime(2025, 12, 31, tzinfo=dt.UTC).timestamp())
        item = MediaItemFactory.build(url=f"https://media.example.com/photos/foo.jpg?Expires={expires}")
        client = mock_client(lambda _request: httpx.Response(403))
        destination = tmp_path / item.path
        with pytest.raises(DownloadError, match="signed URL expired"):
            download(
                partial(client.stream_media, item.url),
                destination,
                item.captured_at
            )
        assert list(destination.parent.iterdir()) == []

    def test_download_expired_invalid_expires(self, tmp_path, mock_client):
        item = MediaItemFactory.build(url="https://media.example.com/photos/foo.jpg?Expires=foo")
        client = mock_client(lambda _request: httpx.Response(403))
        destination = tmp_path / item.path
        with pytest.raises(DownloadError, match="Client error '403 Forbidden' for url") as exc_info:
            download(
                partial(client.stream_media, item.url),
                destination,
                item.captured_at
            )
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
        assert list(destination.parent.iterdir()) == []

    def test_download_truncated(self, tmp_path, mock_client):
        item = MediaItemFactory.build()
        content = b"x" * 100
        def handler(_request):
            return httpx.Response(200, content=content, headers={'Content-Length': '999'})
        client = mock_client(handler)
        destination = tmp_path / item.path
        with pytest.raises(DownloadError, match=re.escape("truncated download (100 of 999 bytes)")):
            download(
                partial(client.stream_media, item.url),
                destination,
                item.captured_at
            )
        assert list(destination.parent.iterdir()) == []

    def test_download_truncated_invalid_content_length(self, tmp_path, mock_client, sent_requests):
        item = MediaItemFactory.build()
        content = b"x" * 100
        def handler(_request):
            return httpx.Response(200, content=content, headers={'Content-Length': 'foo'})
        client = mock_client(handler)
        destination = tmp_path / item.path
        written = download(
                partial(client.stream_media, item.url),
                destination,
                item.captured_at
            )
        assert [str(request.url) for request in sent_requests] == [item.url]
        assert written == len(content)
        assert destination.read_bytes() == content
        assert destination.stat().st_mtime == item.captured_at.timestamp()
        assert list(destination.parent.iterdir()) == [destination]

    def test_download_zero_bytes(self, tmp_path, mock_client):
        item = MediaItemFactory.build()
        content = b''
        client = mock_client(lambda _request: httpx.Response(200, content=content))
        destination = tmp_path / item.path
        with pytest.raises(DownloadError, match="zero-byte download"):
            download(
                partial(client.stream_media, item.url),
                destination,
                item.captured_at
            )
        assert list(destination.parent.iterdir()) == []

    def test_download_http_error(self, tmp_path, mock_client):
        item = MediaItemFactory.build()
        client = mock_client(lambda _request: httpx.Response(404))
        destination = tmp_path / item.path
        with pytest.raises(DownloadError, match="404 Not Found") as exc_info:
            download(
                partial(client.stream_media, item.url),
                destination,
                item.captured_at
            )
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
        assert list(destination.parent.iterdir()) == []

    def test_download_interrupted(self, tmp_path, mock_client):
        item = MediaItemFactory.build()
        def content():
            yield b"x" * 100
            raise httpx.ReadError("connection reset")
        client = mock_client(lambda _request: httpx.Response(200, content=content()))
        destination = tmp_path / item.path
        with pytest.raises(DownloadError, match="connection reset") as exc_info:
            download(
                partial(client.stream_media, item.url),
                destination,
                item.captured_at
            )
        assert isinstance(exc_info.value.__cause__, httpx.ReadError)
        assert list(destination.parent.iterdir()) == []

    @patch("os.utime", side_effect=OSError)
    def test_download_timestamp_failure(self, utime, tmp_path, mock_client, sent_requests):
        item = MediaItemFactory.build()
        content = b"x" * 100
        client = mock_client(lambda _request: httpx.Response(200, content=content))
        destination = tmp_path / item.path
        written = download(
            partial(client.stream_media, item.url),
            destination,
            item.captured_at
        )
        utime.assert_called_once()
        assert [str(request.url) for request in sent_requests] == [item.url]
        assert written == len(content)
        assert destination.read_bytes() == content
        assert destination.stat().st_mtime != item.captured_at.timestamp()
        assert list(destination.parent.iterdir()) == [destination]
