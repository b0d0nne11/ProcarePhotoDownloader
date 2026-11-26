import datetime as dt
import json
from unittest.mock import patch

import httpx
import pytest

from procare_photo_downloader.api import MEDIA_TIMEOUT, ProcareClient
from procare_photo_downloader.config import MAX_PER_PAGE
from procare_photo_downloader.errors import AuthError, ProcareError, RateLimited
from procare_photo_downloader.models import MediaKind
from procare_photo_downloader.ratelimit import TokenBucket
from tests.factories import KidFactory, MediaItemFactory


@pytest.fixture(autouse=True)
def mock_sleep():
    with patch("time.sleep") as sleep:
        yield sleep


@pytest.fixture(autouse=True)
def mock_acquire():
    with patch.object(TokenBucket, "acquire") as acquire:
        yield acquire


class TestProcareClient:

    def test_context_manager(self):
        client = ProcareClient()
        with client as entered_client:
            assert entered_client is client
            assert not client.is_closed
        assert client.is_closed

    def test_token(self, token):
        assert ProcareClient(token=token).token is token

    def test_login(self, credentials, token, mock_client):
        def handler(request):
            assert request.method == "POST"
            assert request.url.path == "/api/web/auth/"
            assert json.loads(request.content) == {
                "email": credentials.email,
                "password": credentials.password.get_secret_value(),
            }
            response = {
                "user": {
                    "email": token.email,
                    "auth_token": token.auth_token,
                }
            }
            return httpx.Response(200, json=response)
        client = mock_client(handler)
        login_token = client.login(credentials)
        assert login_token == token
        assert client.token is login_token
        assert client.credentials is credentials

    @pytest.mark.parametrize(
        "response, message",
        [
            pytest.param(
                httpx.Response(422, json={"errors": ["foo", "bar"]}),
                "foo; bar",
                id="list",
            ),
            pytest.param(
                httpx.Response(422, json={"errors": "foo"}),
                "foo",
                id="string"
            ),
            pytest.param(
                httpx.Response(422, json={"errors": {"foo": "bar"}}),
                "Email and password did not match.",
                id="other_type",
            ),
            pytest.param(
                httpx.Response(422, json={"foo": "bar"}),
                "Email and password did not match.",
                id="missing_key",
            ),
            pytest.param(
                httpx.Response(422, json=["foo"]),
                "Email and password did not match.",
                id="not_subscriptable",
            ),
            pytest.param(
                httpx.Response(422, text="<html>foo</html>"),
                "Email and password did not match.",
                id="not_json",
            ),
            pytest.param(
                httpx.Response(200, json={}),
                "Login succeeded but no auth token was returned.",
                id="empty_json"
            ),
            pytest.param(
                httpx.Response(422),
                "Email and password did not match.",
                id="empty"
            ),
            pytest.param(
                httpx.Response(200, json={"user": {"auth_token": "foo", "mfa_required": "bar"}}),
                "This account requires multi-factor authentication.",
                id="mfa_required",
            ),
        ],
    )
    def test_login_rejected(self, response, message, credentials, mock_client):
        client = mock_client(lambda request: response)
        with pytest.raises(AuthError) as exc_info:
            client.login(credentials)
        assert str(exc_info.value) == message
        assert client.token is None

    def test_login_incomplete(self, mock_client, sent_requests):
        client = mock_client(lambda request: httpx.Response(200))
        with pytest.raises(AuthError) as exc_info:
            client.login(None)
        assert str(exc_info.value) == "Both an email and a password are needed to log in."
        assert sent_requests == []
        assert client.token is None

    def test_login_rate_limited(self, credentials, mock_client):
        client = mock_client(lambda request: httpx.Response(429))
        with pytest.raises(RateLimited) as exc_info:
            client.login(credentials)
        assert str(exc_info.value) == "Rate limited."

    def test_kids(self, token, mock_client):
        kids = KidFactory.build_batch(2)
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/web/parent/kids"
            assert request.headers["Authorization"] == f"Bearer {token.auth_token}"
            response = {
                "kids": [
                    {
                        "id": kid.id,
                        "name": kid.name,
                        "created_at": kid.created_at.isoformat() if kid.created_at else None,
                    }
                    for kid in kids
                ]
            }
            return httpx.Response(200, json=response)
        client = mock_client(handler, token=token)
        assert client.kids() == kids

    @pytest.mark.parametrize(
        "kind, path, filters, key, to_entry",
        [
            pytest.param(
                MediaKind.PHOTO,
                "/api/web/parent/photos",
                [
                    ("filters[photo][kid_ids][]", "{kid_id}"),
                    ("filters[photo][datetime_from]", "2026-01-01T00:00:00Z"),
                    ("filters[photo][datetime_to]", "2026-01-31T23:59:59Z"),
                ],
                "photos",
                lambda item: {
                    "id": item.id,
                    "date": item.captured_at.isoformat(),
                    "main_url": item.url,
                    "caption": item.caption,
                },
                id="photos",
            ),
            pytest.param(
                MediaKind.PHOTO,
                "/api/web/parent/photos",
                [
                    ("filters[photo][kid_ids][]", "{kid_id}"),
                    ("filters[photo][datetime_from]", "2026-01-01T00:00:00Z"),
                    ("filters[photo][datetime_to]", "2026-01-31T23:59:59Z"),
                ],
                "photos",
                lambda item: {
                    "id": item.id,
                    "date": item.captured_at.replace(tzinfo=None).isoformat(),
                    "main_url": item.url,
                    "caption": item.caption,
                },
                id="photos_with_native_timestamp",
            ),
            pytest.param(
                MediaKind.VIDEO,
                "/api/web/parent/videos",
                [
                    ("filters[video][kid_ids][]", "{kid_id}"),
                    ("filters[video][datetime_from]", "2026-01-01T00:00:00Z"),
                    ("filters[video][datetime_to]", "2026-01-31T23:59:59Z"),
                ],
                "videos",
                lambda item: {
                    "id": item.id,
                    "date": item.captured_at.isoformat(),
                    "video_file_url": item.url,
                    "caption": item.caption,
                },
                id="videos",
            ),
            pytest.param(
                MediaKind.ACTIVITY,
                "/api/web/parent/daily_activities/",
                [
                    ("kid_id", "{kid_id}"),
                    ("filters[daily_activity][date_from]", "2026-01-01"),
                    ("filters[daily_activity][date_to]", "2026-01-31"),
                ],
                "daily_activities",
                lambda item: {
                    "id": item.id,
                    "activity_time": item.captured_at.isoformat(),
                    "photo_url": item.url,
                    "comment": item.caption,
                },
                id="activity",
            ),
        ],
    )
    def test_iter_media(self, kind, path, filters, key, to_entry, kid, token, mock_client):
        items = MediaItemFactory.build_batch(2, kind=kind, kid=kid)
        start = dt.date(2026, 1, 1)
        end = dt.date(2026, 1, 31)
        windows = []
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == path
            assert request.headers["Authorization"] == f"Bearer {token.auth_token}"
            assert request.url.params.multi_items() == [
                ("page", "1"),
                ("per_page", str(MAX_PER_PAGE)),
                *[(name, value.format(kid_id=kid.id)) for name, value in filters],
            ]
            response = {key: [to_entry(item) for item in items]}
            return httpx.Response(200, json=response)
        def on_window(*args):
            windows.append(args)
        client = mock_client(handler, token=token)
        result = list(client.iter_media(kid, kind, start, end, on_window))
        assert result == items
        assert windows == [(kid, kind, start, end, len(items))]

    @pytest.mark.parametrize(
        "to_entry",
        [
            pytest.param(
                lambda item: {
                    "id": item.id,
                    "activity_time": item.captured_at.isoformat(),
                    "comment": item.caption,
                },
                id="activity_with_no_photo",
            ),
            pytest.param(
                lambda item: {
                    "id": item.id,
                    "photo_url": item.url,
                    "comment": item.caption,
                },
                id="activity_with_no_captured_at",
            ),
            pytest.param(
                lambda item: {
                    "id": item.id,
                    "activity_time": "foo",
                    "photo_url": item.url,
                    "comment": item.caption,
                },
                id="activity_with_invalid_captured_at",
            ),
            pytest.param(
                lambda item: {
                    "id": item.id,
                    "activity_type": "photo_activity",
                    "activity_time": item.captured_at.isoformat(),
                    "photo_url": item.url,
                    "comment": item.caption,
                },
                id="activity_with_gallery_type",
            ),
        ]
    )
    def test_iter_media_filtering(self, to_entry, kid, token, mock_client):
        items = MediaItemFactory.build_batch(2, kind=MediaKind.ACTIVITY, kid=kid)
        start = dt.date(2026, 1, 1)
        end = dt.date(2026, 1, 31)
        windows = []
        def handler(request):
            assert request.method == "GET"
            assert request.url.path == "/api/web/parent/daily_activities/"
            assert request.headers["Authorization"] == f"Bearer {token.auth_token}"
            assert request.url.params.multi_items() == [
                ("page", "1"),
                ("per_page", str(MAX_PER_PAGE)),
                ("kid_id", kid.id),
                ("filters[daily_activity][date_from]", "2026-01-01"),
                ("filters[daily_activity][date_to]", "2026-01-31"),
            ]
            response = {"daily_activities": [to_entry(item) for item in items]}
            return httpx.Response(200, json=response)
        def on_window(*args):
            windows.append(args)
        client = mock_client(handler, token=token)
        result = list(client.iter_media(kid, MediaKind.ACTIVITY, start, end, on_window))
        assert result == []
        assert windows == [(kid, MediaKind.ACTIVITY, start, end, 0)]

    def test_iter_media_multiple_pages(self, kid, token, mock_client, sent_requests):
        items = MediaItemFactory.build_batch(MAX_PER_PAGE + 1, kind=MediaKind.ACTIVITY, kid=kid)
        start = dt.date(2026, 1, 1)
        end = dt.date(2026, 1, 31)
        windows = []
        def handler(request):
            page = int(request.url.params["page"])
            offset = (page - 1) * MAX_PER_PAGE
            response = {
                "daily_activities": [
                    {
                        "id": item.id,
                        "activity_time": item.captured_at.isoformat(),
                        "photo_url": item.url,
                        "comment": item.caption,
                    }
                    for item in items[offset:offset + MAX_PER_PAGE]
                ]
            }
            return httpx.Response(200, json=response)
        def on_window(*args):
            windows.append(args)
        client = mock_client(handler, token=token)
        result = list(client.iter_media(kid, MediaKind.ACTIVITY, start, end, on_window))
        assert result == items
        assert [request.url.params["page"] for request in sent_requests] == ["1", "2"]
        assert windows == [(kid, MediaKind.ACTIVITY, start, end, len(items))]

    def test_iter_media_split_window(self, kid, token, mock_client, sent_requests):
        items = [
            MediaItemFactory.build(
                kind=MediaKind.ACTIVITY,
                kid=kid,
                captured_at=dt.datetime(2026, 1, day, tzinfo=dt.UTC),
            )
            for day in (2, 1)
        ]
        start = dt.date(2026, 1, 1)
        end = dt.date(2026, 1, 2)
        windows = []
        def handler(request):
            date_from = request.url.params["filters[daily_activity][date_from]"]
            date_to = request.url.params["filters[daily_activity][date_to]"]
            if date_from != date_to:
                raise httpx.ReadTimeout("timed out", request=request)
            response = {
                "daily_activities": [
                    {
                        "id": item.id,
                        "activity_time": item.captured_at.isoformat(),
                        "photo_url": item.url,
                        "comment": item.caption,
                    }
                    for item in items
                    if item.captured_at.date().isoformat() == date_from
                ]
            }
            return httpx.Response(200, json=response)
        def on_window(*args):
            windows.append(args)
        client = mock_client(handler, token=token)
        result = list(
            client.iter_media(kid, MediaKind.ACTIVITY, start, end, on_window, window_days=2)
        )
        assert result == items
        assert [
            (
                request.url.params["filters[daily_activity][date_from]"],
                request.url.params["filters[daily_activity][date_to]"],
            )
            for request in sent_requests
        ] == [
            ("2026-01-01", "2026-01-02"),
            ("2026-01-02", "2026-01-02"),
            ("2026-01-01", "2026-01-01"),
        ]
        assert windows == [
            (kid, MediaKind.ACTIVITY, end, end, 1),
            (kid, MediaKind.ACTIVITY, start, start, 1),
        ]

    def test_iter_media_split_window_exhausted(self, kid, token, mock_client, sent_requests):
        start = dt.date(2026, 1, 1)
        end = dt.date(2026, 1, 2)
        windows = []
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)
        def on_window(*args):
            windows.append(args)
        client = mock_client(handler, token=token)
        with pytest.raises(httpx.ReadTimeout):
            list(client.iter_media(kid, MediaKind.ACTIVITY, start, end, on_window, window_days=2))
        assert [
            (
                request.url.params["filters[daily_activity][date_from]"],
                request.url.params["filters[daily_activity][date_to]"],
            )
            for request in sent_requests
        ] == [
            ("2026-01-01", "2026-01-02"),
            ("2026-01-02", "2026-01-02"),
        ]
        assert windows == []

    @pytest.mark.freeze_time("2026-01-01T00:00:00Z")
    @pytest.mark.parametrize(
        "headers, sleeps",
        [
            pytest.param({}, (2.0, 4.0, 8.0), id="exponential_backoff"),
            pytest.param({"Retry-After": "3"}, (3.0, 3.0, 3.0), id="retry_after"),
            pytest.param({"Retry-After": "0"}, (0.1, 0.1, 0.1), id="retry_after_floor"),
            pytest.param(
                {"Retry-After": "Thu, 01 Jan 2026 00:00:01 GMT"},
                (1.0, 1.0, 1.0),
                id="rfc2822",
            ),
            pytest.param(
                {"Retry-After": "Thu, 01 Jan 2026 00:00:01"},
                (1.0, 1.0, 1.0),
                id="rfc2822_missing_timezone",
            ),
            pytest.param({"Retry-After": "foo"}, (2.0, 4.0, 8.0), id="invalid"),
        ],
    )
    def test_send_rate_limited(
        self, headers, sleeps, token, mock_client, sent_requests, mock_sleep
    ):
        responses = iter([
            httpx.Response(429, headers=headers),
            httpx.Response(429, headers=headers),
            httpx.Response(429, headers=headers),
            httpx.Response(200, json={"kids": []}),
        ])
        client = mock_client(lambda request: next(responses), token=token)
        assert client.kids() == []
        assert len(sent_requests) == 4
        assert mock_sleep.call_args_list == [((v,),) for v in sleeps]

    def test_send_rate_limited_exhausted(self, token, mock_client, mock_sleep):
        client = mock_client(lambda request: httpx.Response(429), token=token)
        with pytest.raises(RateLimited):
            client.kids()
        sleeps = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0)
        assert mock_sleep.call_args_list == [((v,),) for v in sleeps]

    def test_send_relogin(self, credentials, token, mock_client, sent_requests):
        responses = iter([
            httpx.Response(401),
            httpx.Response(200, json={"kids": []}),
        ])
        def handler(request):
            if request.url.path == "/api/web/auth/":
                response = {"user": {"email": token.email, "auth_token": "fresh"}}
                return httpx.Response(200, json=response)
            return next(responses)
        client = mock_client(handler, token=token, credentials=credentials)
        assert client.kids() == []
        assert client.token.auth_token == "fresh"
        assert [request.url.path for request in sent_requests] == [
            "/api/web/parent/kids",
            "/api/web/auth/",
            "/api/web/parent/kids",
        ]

    def test_send_relogin_once(self, credentials, token, mock_client, sent_requests):
        def handler(request):
            if request.url.path == "/api/web/auth/":
                response = {"user": {"email": token.email, "auth_token": "fresh"}}
                return httpx.Response(200, json=response)
            return httpx.Response(401)
        client = mock_client(handler, token=token, credentials=credentials)
        with pytest.raises(AuthError) as exc_info:
            client.kids()
        assert [request.url.path for request in sent_requests] == [
            '/api/web/parent/kids',
            '/api/web/auth/',
            '/api/web/parent/kids',
        ]
        assert str(exc_info.value) == "Not authenticated. Run `procare-photos login`."

    def test_send_no_token(self, mock_client, sent_requests):
        client = mock_client(lambda request: httpx.Response(200, json={"kids": []}))
        with pytest.raises(AuthError) as exc_info:
            client.kids()
        assert sent_requests == []
        assert str(exc_info.value) == "Not authenticated. Run `procare-photos login`."

    def test_send_unauthorized_no_credentials(self, token, mock_client, sent_requests):
        client = mock_client(lambda request: httpx.Response(401), token=token)
        with pytest.raises(AuthError) as exc_info:
            client.kids()
        assert [request.url.path for request in sent_requests] == ['/api/web/parent/kids']
        assert str(exc_info.value) == "Not authenticated. Run `procare-photos login`."

    def test_send_client_error(self, token, mock_client, sent_requests):
        client = mock_client(lambda request: httpx.Response(404, text="x" * 300), token=token)
        with pytest.raises(ProcareError) as exc_info:
            client.kids()
        assert [request.url.path for request in sent_requests] == ['/api/web/parent/kids']
        assert str(exc_info.value) == f"HTTP 404 from /api/web/parent/kids: {'x' * 200}"

    def test_stream_media(self, token, mock_client):
        url = "https://media.example.com/photos/foo.jpg"
        def handler(request):
            assert request.method == "GET"
            assert request.url == url
            assert request.headers["Accept"] == "*/*"
            assert "Authorization" not in request.headers
            assert request.extensions["timeout"] == MEDIA_TIMEOUT.as_dict()
            return httpx.Response(200, content=b"foo")
        client = mock_client(handler, token=token)
        with client.stream_media(url) as response:
            assert response.read() == b"foo"
