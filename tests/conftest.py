import datetime as dt

import httpx
import pytest

from procare_photo_downloader.api import ProcareClient
from tests.factories import CredentialsFactory, KidFactory, MediaItemFactory, TokenFactory


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    """
    Keep every test away from the real token, environment, and ``.env`` file.

    ``Token.path()`` honours ``$XDG_CONFIG_HOME``, so pointing it at a temporary
    directory stops tests from overwriting or deleting the user's cached token.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("PROCARE_EMAIL", raising=False)
    monkeypatch.delenv("PROCARE_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def sent_requests():
    return []


@pytest.fixture
def mock_client(sent_requests):
    def make(handler, token=None, credentials=None):
        def record(request):
            sent_requests.append(request)
            return handler(request)
        return ProcareClient(
            token=token,
            credentials=credentials,
            transport=httpx.MockTransport(record),
        )
    return make


@pytest.fixture
def credentials():
    return CredentialsFactory.build()


@pytest.fixture
def token(credentials):
    return TokenFactory.build(email=credentials.email)


@pytest.fixture
def kid():
    return KidFactory.build(created_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))


@pytest.fixture
def item(kid):
    return MediaItemFactory.build(kid=kid)
