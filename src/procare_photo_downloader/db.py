"""
The database session for one command's run.

A catalog lives in whatever directory ``--out`` names, so unlike a single
database application the engine cannot be built at import time.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlmodel import Session as SQLModelSession

_factory = sessionmaker(class_=SQLModelSession, expire_on_commit=False)
Session = scoped_session(_factory)
CATALOG_FILENAME = ".procare-catalog.sqlite3"
_MIGRATIONS = Path(__file__).parent / "migrations"


def _apply_pragmas(connection: sqlite3.Connection, _record: object) -> None:
    """Settings that have to be set per connection."""
    cursor = connection.cursor()
    # WAL keeps readers from blocking the download threads' writes.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # WAL still serialises writers. Wait for the lock instead of failing.
    cursor.execute("PRAGMA busy_timeout=5000")
    # enforce foreign keys, without this they are just decoration.
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def engine_for(output_dir: Path) -> sa.Engine:
    """An engine for the catalog inside ``output_dir``."""
    path = output_dir / CATALOG_FILENAME
    engine = sa.create_engine(f"sqlite:///{path}")
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def migrate(engine: sa.Engine) -> None:
    """Bring the catalog behind ``engine`` up to the current schema, in place."""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.attributes["engine"] = engine
    command.upgrade(config, "head")


@contextmanager
def session_scope(output_dir: Path) -> Generator[SQLModelSession]:
    """Own the session lifecycle, against a catalog known to be current."""
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = engine_for(output_dir)
    migrate(engine)
    Session.configure(bind=engine)
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        Session.remove()
        engine.dispose()
