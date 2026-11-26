import datetime as dt

from procare_photo_downloader.models import MediaKind
from tests.factories import MediaItemFactory


class TestMediaItem:

    def test_path(self):
        item = MediaItemFactory.build(
            id="1234567890",
            kind=MediaKind.PHOTO,
            captured_at=dt.datetime(2025, 12, 31, 0, 0, 0, tzinfo=dt.UTC),
            url="https://media.example.com/photos/img.png?sig=abc",
            kid__name='Zoë Smith',
        )
        assert str(item.path) == "Zoë Smith/2025/2025-12-31_000000_12345678.png"

    def test_path_missing_suffix(self):
        item = MediaItemFactory.build(
            id="1234567890",
            kind=MediaKind.PHOTO,
            captured_at=dt.datetime(2025, 12, 31, 0, 0, 0, tzinfo=dt.UTC),
            url="https://media.example.com/photos/img?sig=abc",
            kid__name='Zoë Smith',
        )
        assert str(item.path) == "Zoë Smith/2025/2025-12-31_000000_12345678.jpg"
