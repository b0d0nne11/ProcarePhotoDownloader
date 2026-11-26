"""HTTP client for the Procare Connect carer (parent) API.

The public web app talks to an undocumented JSON API. The calls this package
needs are:

- ``POST /auth/``                    exchange email + password for an auth token
- ``GET  /parent/kids``              list the children on the account
- ``GET  /parent/photos``            list media, filtered by child and date range
- ``GET  /parent/daily_activities``  the timeline behind the dashboard

The last of those is not a media endpoint, but staff post photos as
attachments to timeline entries and those never appear in the gallery
listing. See ``MediaKind``.

Media itself lives on a CloudFront distribution and is fetched with pre-signed
URLs that need no ``Authorization`` header.
"""

import datetime as dt
import logging
import time
from collections.abc import Callable, Iterator
from email.utils import parsedate_to_datetime
from typing import Any, Self, assert_never

import httpx

from .config import (
    API_BASE,
    DEFAULT_WINDOW_DAYS,
    MAX_PER_PAGE,
    MIN_WINDOW_DAYS,
    Credentials,
    Token,
)
from .errors import AuthError, ProcareError, RateLimited
from .models import Kid, MediaItem, MediaKind
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

# The listing endpoint gateway-times-out on wide ranges. Requests that take
# this long are treated as a signal to narrow the window, not as a hard error.
LIST_TIMEOUT = httpx.Timeout(connect=10.0, read=75.0, write=10.0, pool=10.0)

# Media is megabytes where a listing is kilobytes, so it gets longer to arrive.
MEDIA_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

# The timeline reports gallery media as well, under these two types. They are
# the records ``/parent/photos`` already returns, but keyed by the activity
# rather than by the photo, so taking them here would fetch the same image a
# second time under a second name.
GALLERY_ACTIVITY_TYPES = frozenset({"photo_activity", "video_activity"})

# A floor on Retry-After, so a server answering "0" cannot turn the retry loop
# into a hot loop. Low enough to stay out of the way of a real value.
MIN_RETRY_AFTER = 0.1


def _get_media_item_entries(kind: MediaKind, payload: dict) -> list[dict]:
    match kind:
        case MediaKind.PHOTO:
            return payload.get("photos", [])
        case MediaKind.VIDEO:
            return payload.get("videos", [])
        case MediaKind.ACTIVITY:
            return payload.get("daily_activities", [])
        case _:  # pragma: no cover
            assert_never(kind)


def _parse_media_items(kind: MediaKind, kid_id: str, entries: list[dict]) -> list[MediaItem]:
    items = []
    for entry in entries:
        if kind == MediaKind.ACTIVITY:
            if not entry.get("photo_url"):
                continue
            if entry.get("activity_type") in GALLERY_ACTIVITY_TYPES:
                continue
        item = _parse_media_item(kind, entry, kid_id)
        if item is not None:
            items.append(item)
    return items


def _parse_media_item(kind: MediaKind, entry: dict, kid_id: str) -> MediaItem | None:
    match kind:
        case MediaKind.PHOTO:
            captured_at = _parse_timestamps(
                entry.get("date"),
                entry.get("created_at"),
            )
            url = entry.get("main_url") or ""
            caption = (entry.get("caption") or "").strip()
        case MediaKind.VIDEO:
            captured_at = _parse_timestamps(
                entry.get("date"),
                entry.get("created_at")
            )
            url = entry.get("video_file_url") or ""
            caption = (entry.get("caption") or "").strip()
        case MediaKind.ACTIVITY:
            captured_at = _parse_timestamps(
                entry.get("activity_time"),
                entry.get("activity_date")
            )
            url = entry.get("photo_url") or ""
            caption = (entry.get("comment") or "").strip()
        case _:  # pragma: no cover
            assert_never(kind)
    if captured_at is None:
        log.warning("skipping %s %s: no usable timestamp", kind, entry.get("id"))
        return None
    return MediaItem(
        id=entry["id"],
        kid_id=kid_id,
        kind=MediaKind(kind),
        captured_at=captured_at,
        url=url,
        caption=caption,
    )


def _parse_kid(payload: dict) -> Kid:
    return Kid(
        id=payload["id"],
        name=payload.get("name", "").strip(),
        created_at=_parse_timestamp(payload.get("created_at")),
    )


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        log.warning('invalid timestamp: %s', value)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed


def _parse_timestamps(*values: str | None) -> dt.datetime | None:
    for value in values:
        if parsed := _parse_timestamp(value):
            return parsed
    return None


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse Retry-After HTTP header.

    RFC 9110 lets ``Retry-After`` carry a delay seconds or an HTTP date. A
    date already past means the wait is over, which the floor turns into a
    token delay rather than a negative one.
    """
    value = (response.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(MIN_RETRY_AFTER, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        log.warning('invalid retry-after: %s', value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return max(MIN_RETRY_AFTER, (parsed - dt.datetime.now(dt.UTC)).total_seconds())


def _parse_error_messages(response: httpx.Response) -> str:
    """Whatever the body says went wrong, or "" if it does not say.

    Procare answers a 400 with ``{"errors": [...]}`` but a 404 with an HTML
    page and a 401 with nothing at all, so every step of the lookup is allowed
    to fail: no JSON, no such key, or a body that cannot be subscripted.
    """
    try:
        errors = response.json()["errors"]
    except (ValueError, KeyError, TypeError):
        return ""
    if isinstance(errors, list):
        return "; ".join(str(item) for item in errors)
    if isinstance(errors, str):
        return errors
    return ""



class ProcareClient:
    """Thin, retrying wrapper around the Procare API."""

    def __init__(
        self,
        token: Token | None = None,
        credentials: Credentials | None = None,
        requests_per_second: float = 1.5,
        max_retries: int = 8,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._credentials = credentials
        self._max_retries = max_retries
        self._bucket = TokenBucket(rate=requests_per_second)
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=LIST_TIMEOUT,
            follow_redirects=True,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def token(self) -> Token | None:
        return self._token

    @property
    def credentials(self) -> Credentials | None:
        return self._credentials

    def login(self, credentials: Credentials | None) -> Token:
        """
        Fetch an authentication token.

        The credentials are kept on the client so ``_send`` can log in again if
        the token is rejected mid-run. An account that requires multi-factor
        authentication is refused, since there is no way to complete the second
        factor from here.
        """
        if not credentials or not credentials.email or not credentials.password:
            raise AuthError("Both an email and a password are needed to log in.")
        response = self._client.post(
            "/auth/",
            json={
                "email": credentials.email,
                "password": credentials.password.get_secret_value(),
            },
        )
        if response.status_code in (401, 422):
            errors = _parse_error_messages(response)
            raise AuthError(errors or "Email and password did not match.")
        if response.status_code == 429:
            raise RateLimited(_parse_retry_after(response))
        response.raise_for_status()

        user = response.json().get("user") or {}
        email = user.get("email") or credentials.email
        auth_token = user.get("auth_token")
        if not auth_token:
            raise AuthError("Login succeeded but no auth token was returned.")
        if user.get("mfa_required"):
            raise AuthError("This account requires multi-factor authentication.")
        self._token = Token(auth_token=auth_token, email=email)
        self._credentials = credentials
        return self._token

    def _send(self, request: httpx.Request) -> httpx.Response:
        """Send with pacing, 429 backoff, and one automatic re-login on 401."""
        attempt = 0
        relogged = False
        while True:
            if not self._token:
                raise AuthError("Not authenticated. Run `procare-photos login`.")
            request.headers["Authorization"] = f"Bearer {self._token.auth_token}"
            self._bucket.acquire()
            try:
                response = self._client.send(request)
                if response.status_code == 401:
                    raise AuthError("Not authenticated. Run `procare-photos login`.")
                if response.status_code == 429:
                    raise RateLimited(_parse_retry_after(response))
                if response.is_client_error:
                    raise ProcareError(
                        f"HTTP {response.status_code} from {request.url.path}: "
                        f"{_parse_error_messages(response) or response.text[:200]}"
                    )
                response.raise_for_status()
            except RateLimited as e:
                attempt += 1
                if attempt > self._max_retries:
                    raise
                self._bucket.backoff()
                delay = e.retry_after or min(60.0, 2.0**attempt)
                log.warning("rate limited: delay=%.1fs rate=%.2f/s", delay, self._bucket.rate)
                time.sleep(delay)
                continue
            except AuthError:
                if relogged:
                    raise
                relogged = True
                if not self._credentials or not self._credentials.complete:
                    raise
                log.info("auth token rejected, re-authenticating")
                self._token = None
                self.login(self._credentials)
                continue
            else:
                self._bucket.recover()
            return response

    def kids(self) -> list[Kid]:
        """The children on the account."""
        request = self._client.build_request("GET", "/parent/kids")
        payload = self._send(request).json()
        return [_parse_kid(entry) for entry in payload.get("kids", [])]

    def iter_media(
        self,
        kid: Kid,
        kind: MediaKind,
        start: dt.date,
        end: dt.date,
        on_window: Callable[[Kid, MediaKind, dt.date, dt.date, int], None],
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> Iterator[MediaItem]:
        """Yield every media item of one kind for a child between two dates.

        The range is swept newest-first in windows of ``window_days``. A window
        that times out is halved and retried, down to a single day, which keeps
        the sweep working even for unusually busy stretches.
        """
        window_end = end
        window_start = max(start, window_end - dt.timedelta(days=window_days - 1))
        while window_end >= start:
            try:
                yield from self._window(
                    kid,
                    kind,
                    window_start,
                    window_end,
                    on_window
                )
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                if window_days <= MIN_WINDOW_DAYS:
                    raise
                log.warning('failed to get window %s..%s (%sd): %s',
                    window_start, window_end, window_days, e)
                yield from self.iter_media(
                    kid,
                    kind,
                    window_start,
                    window_end,
                    on_window,
                    max(MIN_WINDOW_DAYS, window_days // 2),
                )
            window_end = window_start - dt.timedelta(days=1)
            window_start = max(start, window_end - dt.timedelta(days=window_days - 1))

    def _window(
        self,
        kid: Kid,
        kind: MediaKind,
        start: dt.date,
        end: dt.date,
        on_window: Callable[[Kid, MediaKind, dt.date, dt.date, int], None],
    ) -> list[MediaItem]:
        """Page through a single date window and return everything in it.

        Results are buffered rather than streamed so that a timeout on a later
        page cannot re-emit the pages already read when the window is retried
        at a narrower width. Paging stops as soon as a short page arrives.
        """
        collected: list[MediaItem] = []
        page = 1
        while True:
            payload = self._page(kind, kid.id, start, end, page)
            entries = _get_media_item_entries(kind, payload)
            collected.extend(_parse_media_items(kind, kid.id, entries))
            if len(entries) < MAX_PER_PAGE:
                break
            page += 1
        on_window(kid, kind, start, end, len(collected))
        return collected

    def _page(
        self, kind: MediaKind, kid_id: str, since: dt.date, until: dt.date, page: int
    ) -> dict[str, Any]:
        """The request for one page of one date window."""
        match kind:
            case MediaKind.PHOTO:
                path = "/parent/photos"
                filters = (
                    ("filters[photo][kid_ids][]", kid_id),
                    ("filters[photo][datetime_from]", f"{since.isoformat()}T00:00:00Z"),
                    ("filters[photo][datetime_to]", f"{until.isoformat()}T23:59:59Z"),
                )
            case MediaKind.VIDEO:
                path = "/parent/videos"
                filters = (
                    ("filters[video][kid_ids][]", kid_id),
                    ("filters[video][datetime_from]", f"{since.isoformat()}T00:00:00Z"),
                    ("filters[video][datetime_to]", f"{until.isoformat()}T23:59:59Z"),
                )
            case MediaKind.ACTIVITY:
                path = "/parent/daily_activities/"
                filters = (
                    ("kid_id", kid_id),
                    ("filters[daily_activity][date_from]", since.isoformat()),
                    ("filters[daily_activity][date_to]", until.isoformat()),
                )
            case _:  # pragma: no cover
                assert_never(kind)
        params = (("page", str(page)), ("per_page", str(MAX_PER_PAGE)), *filters)
        request = self._client.build_request("GET", path, params=params)
        return self._send(request).json()

    def stream_media(self, url: str):
        """Context manager yielding a streaming response for a pre-signed CDN URL.

        The client's defaults are the API's, so media overrides the two that
        do not fit: a longer read for something measured in megabytes, and an
        ``Accept`` that does not claim to want JSON.
        """
        return self._client.stream(
            "GET", url, timeout=MEDIA_TIMEOUT, headers={"Accept": "*/*"}
        )
