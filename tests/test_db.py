import pytest
from sqlmodel import select

from procare_photo_downloader.db import session_scope
from procare_photo_downloader.models import Kid


def test_session_scope_commits(tmp_path, kid):
    with session_scope(tmp_path) as session:
        session.add(kid)
    with session_scope(tmp_path) as session:
        assert session.exec(select(Kid)).one().id == kid.id


def test_session_scope_rolls_back_on_error(tmp_path, kid):
    with pytest.raises(RuntimeError), session_scope(tmp_path) as session:
        session.add(kid)
        session.flush()
        raise RuntimeError("boom")
    with session_scope(tmp_path) as session:
        assert session.exec(select(Kid)).all() == []
