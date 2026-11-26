import csv
import datetime as dt
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from procare_photo_downloader import __version__
from procare_photo_downloader.api import ProcareClient
from procare_photo_downloader.cli import app
from procare_photo_downloader.config import Token
from procare_photo_downloader.db import session_scope
from procare_photo_downloader.errors import AuthError, ProcareError
from procare_photo_downloader.models import Kid, MediaItem, MediaKind
from procare_photo_downloader.sync import Syncer, SyncStats
from tests.factories import KidFactory, MediaItemFactory


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture
def output_dir(tmp_path):
    return tmp_path / "library"


@pytest.fixture
def invoke(output_dir):
    def run(*args, input=None, exit_code=0):
        result = CliRunner().invoke(app, ["--out", str(output_dir), "-v", *args], input=input)
        assert result.exit_code == exit_code, result.output
        return result
    return run


@pytest.mark.parametrize("command", ["kids", "sync"])
class TestAuthentication:

    @patch.object(ProcareClient, "kids")
    def test_not_logged_in(self, mock_kids, invoke, command):
        result = invoke(command, exit_code=2)
        assert "Not logged in." in result.output
        mock_kids.assert_not_called()

    @patch.object(Syncer, "run", new=Mock(return_value=SyncStats()))
    @patch.object(ProcareClient, "kids")
    @patch.object(ProcareClient, "login")
    def test_env_credentials(
        self, mock_login, mock_kids, invoke, monkeypatch, token, credentials, kid, command
    ):
        mock_login.return_value = token
        mock_kids.return_value = [kid]
        monkeypatch.setenv("PROCARE_EMAIL", credentials.email)
        monkeypatch.setenv("PROCARE_PASSWORD", credentials.password.get_secret_value())
        invoke(command)
        mock_login.assert_called_once_with(credentials)
        mock_kids.assert_called_once_with()
        assert Token.load() == token


class TestLogin:

    @patch.object(ProcareClient, "login")
    def test_login(self, mock_login, invoke, token, credentials):
        mock_login.return_value = token
        result = invoke(
            "login",
            "--email", credentials.email,
            "--password", credentials.password.get_secret_value(),
        )
        mock_login.assert_called_once_with(credentials)
        assert Token.load() == token
        assert f"Logged in as {token.email}." in result.output

    @patch.object(ProcareClient, "login")
    def test_login_with_prompt(self, mock_login, invoke, token, credentials):
        mock_login.return_value = token
        result = invoke(
            "login",
            input=f"{credentials.email}\n{credentials.password.get_secret_value()}\n",
        )
        mock_login.assert_called_once_with(credentials)
        assert "Procare email" in result.output
        assert "Procare password: \n" in result.output
        assert Token.load() == token

    @patch.object(ProcareClient, "login")
    def test_login_with_env(self, mock_login, invoke, monkeypatch, token, credentials):
        mock_login.return_value = token
        monkeypatch.setenv("PROCARE_EMAIL", credentials.email)
        monkeypatch.setenv("PROCARE_PASSWORD", credentials.password.get_secret_value())
        result = invoke("login")
        mock_login.assert_called_once_with(credentials)
        assert "Procare email" not in result.output
        assert "Procare password" not in result.output

    @patch.object(ProcareClient, "login")
    def test_login_with_env_and_password_prompt(
        self, mock_login, invoke, monkeypatch, token, credentials
    ):
        mock_login.return_value = token
        monkeypatch.setenv("PROCARE_EMAIL", credentials.email)
        result = invoke(
            "login",
            input=f"{credentials.password.get_secret_value()}\n",
        )
        mock_login.assert_called_once_with(credentials)
        assert "Procare email" not in result.output
        assert "Procare password: \n" in result.output

    @patch.object(ProcareClient, "login")
    def test_login_failed(self, mock_login, invoke, token, credentials):
        mock_login.side_effect = AuthError("Email and password did not match.")
        result = invoke(
            "login",
            "--email", credentials.email,
            "--password", credentials.password.get_secret_value(),
            exit_code=1,
        )
        assert Token.load() is None
        assert "Login failed: Email and password did not match." in result.output


class TestCatalog:

    @pytest.mark.parametrize("command", ["logout", "version", "kids"])
    @patch.object(ProcareClient, "kids")
    def test_catalog_is_lazy(self, mock_kids, invoke, output_dir, kid, token, command):
        mock_kids.return_value = [kid]
        token.save()
        invoke(command)
        assert not output_dir.exists()

    def test_catalog_is_created(self, invoke, output_dir):
        invoke("status")
        assert (output_dir / ".procare-catalog.sqlite3").is_file()


class TestLogout:

    def test_logout(self, invoke, token):
        token.save()
        result = invoke("logout")
        assert Token.load() is None
        assert "Cached token removed." in result.output

    def test_logout_without_token(self, invoke):
        result = invoke("logout")
        assert Token.load() is None
        assert "Cached token removed." in result.output


class TestKids:

    @patch.object(ProcareClient, "kids")
    def test_kids(self, mock_kids, invoke, kid, token):
        mock_kids.return_value = [kid]
        token.save()
        result = invoke("kids")
        assert kid.name in result.output


class TestSync:

    @patch.object(Syncer, "run", return_value=SyncStats(discovered=3, downloaded=2))
    @patch.object(ProcareClient, "kids")
    def test_sync(self, mock_kids, mock_run, invoke, output_dir, token, kid):
        mock_kids.return_value = [kid]
        token.save()
        _result = invoke("sync", "--since", "2026-01-01", "--until", "2026-01-31")
        mock_run.assert_called_once_with(
            [kid],
            [MediaKind.PHOTO, MediaKind.VIDEO, MediaKind.ACTIVITY],
            dt.date(2026, 1, 1),
            dt.date(2026, 1, 31),
            dry_run=False,
        )
        with session_scope(output_dir) as session:
            assert session.get_one(Kid, kid.id).name == kid.name

    @pytest.mark.parametrize(
        ("args", "scope"),
        [
            ((), "Scanning:"),
            (("--since", "2026-01-01"), "Scanning from 2026-01-01:"),
            (("--until", "2026-01-31"), "Scanning up to 2026-01-31:"),
            (("--since", "2026-01-01", "--until", "2026-01-31"),
             "Scanning from 2026-01-01 to 2026-01-31:"),
        ],
    )
    @patch.object(Syncer, "run", return_value=SyncStats())
    @patch.object(ProcareClient, "kids")
    def test_sync_scope(self, mock_kids, _mock_run, invoke, token, kid, args, scope):
        mock_kids.return_value = [kid]
        token.save()
        result = invoke("sync", *args)
        assert f"{scope}\n  {kid.name}\n" in result.output

    @pytest.mark.parametrize("option", ["--since", "--until"])
    @patch.object(Syncer, "run")
    def test_sync_invalid_date(self, mock_run, invoke, token, option):
        token.save()
        result = invoke("sync", option, "2026-13-01", exit_code=2)
        assert "Invalid date '2026-13-01'. Use YYYY-MM-DD." in result.output
        mock_run.assert_not_called()

    @patch.object(Syncer, "run")
    def test_sync_since_after_until(self, mock_run, invoke, token):
        token.save()
        result = invoke("sync", "--since", "2026-02-01", "--until", "2026-01-31", exit_code=2)
        assert "--since must not be after --until" in result.output
        mock_run.assert_not_called()

    @patch.object(Syncer, "run")
    @patch.object(ProcareClient, "kids")
    def test_sync_kids_failed(self, mock_kids, mock_run, invoke, token):
        mock_kids.side_effect = ProcareError("Server error.")
        token.save()
        result = invoke("sync", exit_code=1)
        assert "Server error." in result.output
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "select",
        [
            pytest.param(lambda kid: kid.id, id="id"),
            pytest.param(lambda kid: "JANE", id="name"),
        ],
    )
    @patch.object(Syncer, "run", return_value=SyncStats())
    @patch.object(ProcareClient, "kids")
    def test_sync_select_kid(self, mock_kids, mock_run, invoke, token, select):
        jane = KidFactory.build(name="Doe, Jane")
        john = KidFactory.build(name="Doe, John")
        mock_kids.return_value = [jane, john]
        token.save()
        invoke("sync", "--kid", select(jane))
        assert mock_run.call_args.args[0] == [jane]

    @patch.object(Syncer, "run")
    @patch.object(ProcareClient, "kids")
    def test_sync_select_kid_no_match(self, mock_kids, mock_run, invoke, token, kid):
        mock_kids.return_value = [kid]
        token.save()
        result = invoke("sync", "--kid", "nobody")
        assert "No child matches ['nobody']." in result.output
        mock_run.assert_not_called()

    @patch.object(Syncer, "run", return_value=SyncStats())
    @patch.object(ProcareClient, "kids")
    def test_sync_without_kinds(self, mock_kids, mock_run, invoke, token, kid):
        mock_kids.return_value = [kid]
        token.save()
        invoke("sync", "--no-photos", "--no-activity-photos")
        assert mock_run.call_args.args[1] == [MediaKind.VIDEO]

    @patch.object(Syncer, "run", return_value=SyncStats(discovered=3))
    @patch.object(ProcareClient, "kids")
    def test_sync_dry_run(self, mock_kids, mock_run, invoke, token, kid):
        mock_kids.return_value = [kid]
        token.save()
        result = invoke("sync", "--dry-run")
        assert mock_run.call_args.kwargs == {"dry_run": True}
        assert "Dry run: nothing was downloaded." in result.output

    @patch.object(Syncer, "run", autospec=True)
    @patch.object(ProcareClient, "kids")
    def test_sync_progress_and_report(self, mock_kids, mock_run, invoke, token, kid, item):
        def fake_run(syncer, kids, kinds, since, until, dry_run):
            syncer.hooks.on_window(kid, MediaKind.PHOTO, since, until, 1)
            syncer.hooks.on_discovered(kid, MediaKind.ACTIVITY, 2)
            syncer.hooks.on_download_start(2)
            syncer.hooks.on_download_done()
            syncer.hooks.on_download_failed(item, "HTTP 500")
            return SyncStats(
                discovered=2,
                adopted=1,
                failed=1,
                skipped_unavailable=1,
                bytes_written=1000,
            )
        mock_run.side_effect = fake_run
        mock_kids.return_value = [kid]
        token.save()
        result = invoke("sync", "--since", "2026-01-01", "--until", "2026-01-31")
        assert "Scanning from 2026-01-01 to 2026-01-31:" in result.output
        assert f"  {kid.name}: found 2 activity photos" in result.output
        assert f"  failed {item.id}: HTTP 500" in result.output
        summary = {
            tuple(cell.strip() for cell in line.strip("│").split("│"))
            for line in result.output.splitlines() if line.startswith("│")
        }
        assert summary == {
            ("Discovered", "2"),
            ("Already present", "0"),
            ("Adopted", "1"),
            ("Failed", "1"),
            ("Skipped unavailable", "1"),
            ("Bytes written", "1.0 kB"),
        }


class TestVerify:

    def test_verify(self, invoke, output_dir, kid):
        downloaded_at = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
        present = MediaItemFactory.build(
            kid=kid,
            local_path="present.jpg",
            size_bytes=100,
            downloaded_at=downloaded_at,
            attempts=3,
        )
        missing = MediaItemFactory.build(
            kid=kid,
            local_path="missing.jpg",
            size_bytes=100,
            downloaded_at=downloaded_at,
            attempts=3,
        )
        with session_scope(output_dir) as session:
            session.add_all([present, missing])
            session.commit()
        (output_dir / present.local_path).write_bytes(b"x" * 100)

        result = invoke("verify")

        with session_scope(output_dir) as session:
            catalog = {item.id: item for item in session.exec(MediaItem.iter_all())}

        assert catalog[present.id].local_path == "present.jpg"
        assert catalog[present.id].size_bytes == 100
        assert catalog[present.id].downloaded_at == downloaded_at
        assert catalog[present.id].attempts == 3

        assert catalog[missing.id].local_path is None
        assert catalog[missing.id].size_bytes is None
        assert catalog[missing.id].downloaded_at is None
        assert catalog[missing.id].attempts == 0

        assert "Re-queued 1 item(s) missing from disk." in result.output


class TestResetFailed:

    def test_reset_failed(self, invoke, kid, output_dir):
        item = MediaItemFactory.build(kid=kid, attempts=3)
        with session_scope(output_dir) as session:
            session.add(item)
            session.commit()
        result = invoke("reset-failed")
        with session_scope(output_dir) as session:
            catalog = {item.id: item for item in session.exec(MediaItem.iter_all())}
        assert catalog[item.id].attempts == 0
        assert "Reset the failure count on 1 item(s)." in result.output


class TestStatus:

    def test_status(self, invoke, kid, output_dir):
        captured_at = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
        items = [
            MediaItemFactory.build(
                kid=kid,
                captured_at=captured_at,
                size_bytes=1000,
                downloaded_at=captured_at,
            ),
            MediaItemFactory.build(
                kid=kid,
                captured_at=captured_at,
                attempts=1
            ),
        ]
        with session_scope(output_dir) as session:
            session.add_all(items)
            session.commit()
        result = invoke("status")
        rows = {
            tuple(cell.strip() for cell in line.strip("│").split("│"))
            for line in result.output.splitlines() if line.startswith("│")
        }
        assert rows == {
            (
                kid.name,
                "photo",
                "1 / 2",
                "1",
                "1.0 kB",
                "2026-01-02",
                "2026-01-02",
            ),
        }


class TestExport:

    def test_export(self, invoke, kid, output_dir, tmp_path):
        item = MediaItemFactory.build(kid=kid, caption="At the park")
        with session_scope(output_dir) as session:
            session.add(item)
            session.commit()
        dest = tmp_path / "media.csv"
        result = invoke("export", "--to", str(dest))
        with dest.open() as fh:
            assert list(csv.DictReader(fh)) == [
                {
                    "id": item.id,
                    "kid_name": item.kid.name,
                    "kind": "photo",
                    "captured_at": item.captured_at.isoformat(),
                    "caption": "At the park",
                    "local_path": "",
                    "size_bytes": "",
                    "last_error": "",
                },
            ]
        assert "Wrote 1 row(s)" in result.output


class TestVersion:

    def test_version(self, invoke):
        result = invoke("version")
        assert result.output.strip() == __version__
