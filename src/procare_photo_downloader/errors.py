"""Exception hierarchy for the downloader."""


class ProcareError(Exception):
    """Base class for every error raised by this package."""


class AuthError(ProcareError):
    """Credentials were rejected, or the stored token is no longer valid."""


class RateLimited(ProcareError):
    """The API returned 429."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("Rate limited.")
        self.retry_after = retry_after


class DownloadError(ProcareError):
    """A media file could not be retrieved from the CDN."""
