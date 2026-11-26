"""
Env based configuration and token caching.

Credentials are never written to disk by this package. Only the short-lived
auth token is cached, in a 0600 file under the user's config directory.
"""

from pathlib import Path
from typing import Any, Self

import platformdirs
from pydantic import BaseModel, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "procare-photo-downloader"

API_BASE = "https://api-school.procareconnect.com/api/web"

# Empirically derived limits. See README for how these were measured.
MAX_PER_PAGE = 50
DEFAULT_WINDOW_DAYS = 60
MIN_WINDOW_DAYS = 1


class Credentials(BaseSettings):
    """
    Procare login details, read from ``PROCARE_*`` or a ``.env`` file.

    Both halves are optional so that a partial configuration is still useful:
    setting only ``PROCARE_EMAIL``, which is the sensible thing to do when you
    would rather not keep a password in a file, spares you retyping the address
    while still prompting for the password.
    """

    model_config = SettingsConfigDict(
        env_prefix="PROCARE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # A half filled in after the fact, from a prompt say, has to be coerced
        # the same way a constructor argument would be. Without this a plain
        # string assigned to ``password`` would stay a string.
        validate_assignment=True,
    )

    email: str | None = None
    password: SecretStr | None = None

    @classmethod
    def resolve(cls, email: str | None = None, password: str | None = None) -> Self:
        """Resolve credentials from arguments or environment values."""
        given = {"email": email, "password": password}
        kwargs: dict[str, Any] = {k: v for k, v in given.items() if v}  # drop any empty kwargs
        return cls(**kwargs)

    @property
    def complete(self) -> bool:
        """Whether both halves are present, so a login can be attempted."""
        return bool(self.email and self.password)


class Token(BaseModel):
    """The cached auth token."""

    auth_token: str
    email: str | None = None

    @classmethod
    def path(cls) -> Path:
        """Where the token is cached, wherever the platform puts such things.

        ``$XDG_CONFIG_HOME`` still wins where it is set, on macOS as well as
        Linux, which is what the tests rely on to keep out of a real one.
        """
        return Path(platformdirs.user_config_dir(APP_NAME)) / "token.json"

    @classmethod
    def clear(cls) -> None:
        """Clear the token file."""
        cls.path().unlink(missing_ok=True)

    def save(self) -> None:
        """Save the token with owner-only permissions."""
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)  # in case a file was left behind from an earlier run
        path.write_text(self.model_dump_json(), encoding="utf-8")

    @classmethod
    def load(cls) -> Self | None:
        """Load the token."""
        try:
            return cls.model_validate_json(cls.path().read_bytes())
        except (OSError, ValidationError):
            return None
