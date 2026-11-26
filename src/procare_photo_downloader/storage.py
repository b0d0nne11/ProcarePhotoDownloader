"""Atomic media writes."""

import datetime as dt
import os
from pathlib import Path

import httpx

from .errors import DownloadError

PARTIAL_SUFFIX = ".part"


def download(
    response_factory,
    destination: Path,
    captured_at: dt.datetime,
    chunk_size: int = 1 << 16,
) -> int:
    """Stream one item to ``destination`` atomically. Returns bytes written."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)

    written = 0
    try:
        with response_factory() as response:
            if response.status_code == 403 and _is_expired(response):
                raise DownloadError('signed URL expired; re-run sync to refresh it')
            response.raise_for_status()

            content_length = _content_length(response)
            with open(partial, "wb") as fh:
                for chunk in response.iter_bytes(chunk_size):
                    fh.write(chunk)
                    written += len(chunk)

            if content_length is not None and written != content_length:
                raise DownloadError(f"truncated download ({written} of {content_length} bytes)")
            if written == 0:
                raise DownloadError("zero-byte download")
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise DownloadError(str(exc)) from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    _apply_timestamp(partial, captured_at)
    partial.replace(destination)
    return written


def _is_expired(response: httpx.Response) -> bool:
    """
    Distinguish an expired signature from media that no longer exists.

    CloudFront answers both with 403, but the signature carries its own expiry,
    so a URL that is valid and still refused means the underlying file is gone.
    A meaningful share of older media on Procare is in that state, and it fails
    the same way in their own web app.
    """
    params = response.request.url.params
    if 'Expires' in params:
        try:
            stamp = int(params["Expires"])
            expires = dt.datetime.fromtimestamp(stamp, dt.UTC)
            if expires < dt.datetime.now(dt.UTC):
                return True
        except (ValueError, OverflowError, OSError):
            pass
    return False


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("Content-Length")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _apply_timestamp(path: Path, captured_at: dt.datetime) -> None:
    """Set the file's mtime to when the photo was taken.

    Photo libraries and backup tools sort by mtime. Without this every
    downloaded file would appear to have been created today. This function never
    fails so that we don't lose an item due to a weird clock issue.
    """
    try:
        mtime = captured_at.timestamp()
        os.utime(path, (mtime, mtime))
    except (OSError, OverflowError, ValueError):
        pass
