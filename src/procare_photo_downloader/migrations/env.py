"""Alembic environment.

There is no single database here. Every output directory carries its own
catalog, so the engine is handed in by ``db.migrate`` at runtime rather than read
from a URL in a config file. The URL fallback exists only so a maintainer can
run ``alembic revision --autogenerate`` against a scratch file.
"""

from alembic import context
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from procare_photo_downloader import models  # noqa: F401  (registers the tables)

config = context.config
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = config.attributes.get("engine")
    if engine is None:
        url = config.get_main_option("sqlalchemy.url")
        if not url:
            raise RuntimeError("No engine given and alembic.ini doesn't define sqlalchemy.url.")
        engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot alter a column in place, so every op that needs to
            # goes through a table rebuild.
            render_as_batch=True,
            # SQLite types are advisory. Comparing them only produces churn:
            # SQLModel maps ``str`` to VARCHAR while the tables are declared
            # TEXT, and the column holds the same values either way.
            compare_type=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
